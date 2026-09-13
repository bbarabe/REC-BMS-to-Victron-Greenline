"""REC's policy boundary, durable accounting and sole protocol relay writer."""
import os
import hashlib
import time

from control_inputs import DemandModel, SourceRegistry, LoadServiceEvidence, TimedMean, finite, normalize_bus_snapshot, selected_battery_matches
from energy_accounting import EnergyLedger
from policy_contract import PolicyContract, TransferSupervisor, dumps
from rec_control_config import ControlConfig
from policy_telemetry import PolicyTelemetry

BUSITEM = 'com.victronenergy.BusItem'
PREFIX = '/RecBms/Policy/'


class RecPolicyAdapter:
    def __init__(self, driver, clock=None):
        self.driver = driver
        self.clock = clock or time
        cfg = driver.cfg
        self.config = ControlConfig(getattr(cfg, 'policy_parameters', None))
        self.ledger = EnergyLedger(capacity_ah=cfg.installed_ah,
                                   capacity_version=getattr(cfg, 'capacity_version', 'rated-1440'),
                                   calendar_timezone=cfg.policy_calendar_timezone,
                                   path=getattr(cfg, 'policy_state_path', os.path.join(os.path.dirname(__file__), 'solar-control-state.json')),
                                   max_gap_s=self.config.source_gap_s,
                                   max_overhead_efc=self.config.ordinary_overhead_efc,
                                   reverse_event_wh=self.config.reverse_event_wh,
                                   reverse_day_wh=self.config.reverse_day_wh)
        self.contract = PolicyContract(self.ledger.controller_state.setdefault('contract', {}))
        self.transfer = TransferSupervisor(self.ledger.controller_state.setdefault('transfer', {}),
            connected_dwell_s=self.config.connected_dwell_s, timeout_s=self.config.transfer_timeout_s,
            hourly_departures=int(self.config.hourly_departures), daily_departures=int(self.config.daily_departures),
            backoff_s=self.config.failed_probe_backoff_s)
        self.last_boost_id = None
        self.sources = SourceRegistry(max_age_s=10)
        self.demand_model = DemandModel(self.config.inverter_efficiency, self.config.inverter_idle_w,
                                        self.config.demand_uncertainty_w)
        configuration = {'control': vars(self.config), 'driver': {
            key: value for key, value in vars(cfg).items()
            if key not in ('policy_state_path', 'log_level')}}
        self.configuration_id = hashlib.sha256(dumps(configuration).encode()).hexdigest()[:12]
        self.sources_names = {}
        self.sense_capable_sources = set()
        self.last_poll = None
        self.discovery_at = None
        self.discovery_names = None
        self.read_issued = {}
        self.read_applied = {}
        self.read_pending = {}
        self.poll_diagnostics = {}
        self.last_measurement_stamp = None
        self.measurement_was_valid = None
        self.control = {}
        self.last_snapshot = {}
        self.last_connected = None
        self.attribution_observation = None
        self.attribution_island_since = None
        self.attribution_last_now = None
        self.recovery_since = None
        self.load_service = LoadServiceEvidence(self.config.stable_admission_s,
            self.config.reverse_response_s, self.config.reverse_power_w, self.config.source_gap_s)
        self.pv_activity = TimedMean(self.config.stable_admission_s)
        self.pv_activity_last = None
        self.telemetry = PolicyTelemetry(('system', 'vebus') +
            tuple('solar%d' % instance for instance in cfg.policy_mppt_instances))
        self._register()

    def _register(self):
        service = self.driver.batt
        for name, value in (('Version', 2), ('Generation', self.contract.generation),
                            ('Status', '{}'), ('Snapshot', '{}'), ('Owned', int(self.contract.owned))):
            service.add_path(PREFIX + name, value)
        service.add_path(PREFIX + 'Request', '', writeable=True, onchangecallback=self._requested)
        self.telemetry.register(service)

    def _new_consumer_present(self):
        cfg = self.driver.cfg
        name = 'com.victronenergy.switch.' + cfg.policy_consumer_suffix
        bus = self.driver.sbus
        if name not in bus.list_names():
            return False
        try:
            instance = bus.call_blocking(name, '/DeviceInstance', BUSITEM,
                                         'GetValue', '', [], timeout=1)
            version = bus.call_blocking(name, '/SolarPriority/ProtocolVersion', BUSITEM,
                                        'GetValue', '', [], timeout=1)
            return instance == cfg.policy_consumer_instance and version == 2
        except Exception:
            return False

    def _requested(self, path, payload):
        driver = self.driver
        now = self.clock.monotonic()
        target = float(driver.settings['chargeslider'])
        previously_owned = self.contract.owned
        accepted = self.contract.accept(payload, now, target,
                                        consumer_ready=self.contract.owned or self._new_consumer_present())
        if accepted:
            if not previously_owned and not self.ledger.save():
                self.contract.rejection = 'policy persistence failed'
                self.contract.expires = now
                accepted = False
        driver.batt[PREFIX + 'Owned'] = int(self.contract.owned)
        return accepted

    def _poll(self, now):
        if self.last_poll is not None and now - self.last_poll < 1:
            return
        self.last_poll = now
        driver = self.driver
        names = [str(n) for n in driver.sbus.list_names() if str(n).startswith('com.victronenergy.')]
        discover = self.discovery_names != set(names) or self.discovery_at is None or now-self.discovery_at >= 30
        selected = dict(self.sources_names)
        if discover:
            self.discovery_names, self.discovery_at = set(names), now
            selected = {'system': next((n for n in names if n == 'com.victronenergy.system'), None),
                    'vebus': driver._find_vebus()}
            arrays = getattr(driver.cfg, 'policy_mppt_instances', (278, 279))
            solar_names = [n for n in names if n.startswith('com.victronenergy.solarcharger.')]
            for instance in arrays:
                selected['solar%d' % instance] = next((n for n in solar_names if
                    driver._read_number(n, '/DeviceInstance') == instance), None)
        for role, previous in self.sources_names.items():
            if previous != selected.get(role) and previous:
                self.sources.remove(previous)
                self.read_pending.pop(role, None)
        self.sources_names = selected
        for role, name in selected.items():
            if not name:
                continue
            if role in self.read_pending:
                continue
            sequence = self.read_issued.get(role, 0) + 1
            self.read_issued[role] = sequence
            self.read_pending[role] = (name, sequence)
            epoch = self.sources.generation(name)
            self.poll_diagnostics.setdefault(role, {}).update(issued=sequence, issued_at=now, epoch=epoch)
            def received(values, source=name, source_role=role, request_sequence=sequence,
                         request_epoch=epoch, requested_at=now):
                if self.read_pending.get(source_role) == (source, request_sequence):
                    self.read_pending.pop(source_role, None)
                if (self.sources_names.get(source_role) != source or self.sources.generation(source) != request_epoch or
                        request_sequence <= self.read_applied.get(source_role, 0)):
                    self.poll_diagnostics.setdefault(source_role, {}).update(discarded=request_sequence, reply_at=self.clock.monotonic(), current_epoch=self.sources.generation(source))
                    return
                self.poll_diagnostics.setdefault(source_role, {}).update(applied=request_sequence, reply_at=self.clock.monotonic(), error='')
                self.read_applied[source_role] = request_sequence
                values = dict(values)
                paths = ('/Ac/Consumption/L1/Power', '/Dc/System/Power', '/Dc/System/MeasurementType',
                         '/Dc/Pv/Power', '/Dc/Pv/Current', '/ActiveBatteryService', '/ActiveBmsService',
                         '/Dc/Battery/BatteryService', '/ActiveBmsInstance', '/Control/EffectiveChargeVoltage') if source_role == 'system' else (
                         '/Connected', '/Ac/ActiveIn/ActiveInput', '/Ac/Out/L1/P', '/Dc/0/Power', '/Dc/0/Current',
                         '/Dc/0/Voltage', '/BatteryOperationalLimits/MaxChargeCurrent',
                         '/BatteryOperationalLimits/MaxChargeVoltage', '/Dc/0/MaxChargeCurrent')
                if source_role.startswith('solar'):
                    paths = ('/Yield/Power', '/MppOperationMode', '/Connected', '/Dc/0/Current',
                             '/Dc/0/Voltage', '/Link/ChargeVoltage', '/Link/ChargeCurrent',
                             '/Settings/ChargeCurrentLimit', '/Link/VoltageSense',
                             '/Link/VoltageSenseActive')
                values = normalize_bus_snapshot(values)
                if source_role.startswith('solar') and '/Link/VoltageSenseActive' in values:
                    self.sense_capable_sources.add(source)
                for path in paths:
                    value = values.get(path)
                    self.sources.observe(source, path, value, requested_at)
            def failed(error, source=name, source_role=role, request_sequence=sequence, request_epoch=epoch):
                if self.read_pending.get(source_role) == (source, request_sequence):
                    self.read_pending.pop(source_role, None)
                self.poll_diagnostics.setdefault(source_role, {}).update(error=str(error), failed=request_sequence, failure_at=self.clock.monotonic())
                if (self.sources_names.get(source_role) == source and self.sources.generation(source) == request_epoch and
                        request_sequence > self.read_applied.get(source_role, 0)):
                    self.read_applied[source_role] = request_sequence
                    self.sources.remove(source)
            try:
                driver.sbus.call_async(name, '/', BUSITEM, 'GetValue', '', [],
                                       reply_handler=received, error_handler=failed, timeout=2)
            except Exception as exc:
                failed(exc)

    def _value(self, role, path, now):
        return self.sources.get(self.sources_names.get(role), path, now)

    def _sense_required(self, role):
        """Once exposed, a remote-sense selector may never become optional.

        Retain capability across source outages/reappearance; a missing or stale
        selector cannot establish that a charger reverted to its local sensor.
        """
        name = self.sources_names.get(role)
        capable = getattr(self, 'sense_capable_sources', None)
        if capable is None:
            capable = self.sense_capable_sources = set()
        observed = self.sources.samples.get((name, '/Link/VoltageSenseActive'))
        if observed and observed.get('value') is not None:
            capable.add(name)
        return name in capable

    def _quattro_dc_w(self, now):
        """Signed VE.Bus DC power, matching Victron's own systemcalc derivation.

        The reported `/Dc/0/Power` field is approximately AC-in minus AC-out and
        retains a residual (25-26 W observed 2026-09-12) even at 0.0 A measured
        current; it is not net DC power. Derive it instead from fresh, coherent
        `/Dc/0/Voltage` * `/Dc/0/Current` (positive charging the battery, negative
        inverting/drawing from it). Missing or non-finite inputs stay invalid.
        """
        voltage = finite(self._value('vebus', '/Dc/0/Voltage', now))
        current = finite(self._value('vebus', '/Dc/0/Current', now))
        if voltage is None or current is None:
            return None
        return voltage * current

    def _actuator_inputs(self, now):
        """Actual fresh charger observations, independent of requested outputs."""
        solar = []
        for instance in self.driver.cfg.policy_mppt_instances:
            role = 'solar%d' % instance
            fields = {'power_w': '/Yield/Power', 'current_a': '/Dc/0/Current',
                      'voltage_v': '/Dc/0/Voltage', 'voltage_limit_v': '/Link/ChargeVoltage',
                      'current_limit_a': '/Link/ChargeCurrent',
                      'maximum_current_a': '/Settings/ChargeCurrentLimit',
                      'voltage_sense_v': '/Link/VoltageSense',
                      'voltage_sense_active': '/Link/VoltageSenseActive'}
            sample = {key: finite(self._value(role, path, now)) for key, path in fields.items()}
            sample['tracking'] = self._value(role, '/MppOperationMode', now)
            active = sample['voltage_sense_active']
            sense_valid = ((not self._sense_required(role) or active in (0, 1)) and
                           (active != 1 or (sample['voltage_sense_v'] is not None and
                                            sample['voltage_sense_v'] > 0)))
            sample['valid'] = (self._value(role, '/Connected', now) == 1 and
                sample['tracking'] is not None and sense_valid and all(sample[key] is not None for key in
                    ('power_w', 'current_a', 'voltage_v', 'voltage_limit_v', 'current_limit_a')))
            solar.append(sample)
        return {'quattro_v': finite(self._value('vebus', '/BatteryOperationalLimits/MaxChargeVoltage', now)),
                'quattro_current_limit_a': finite(self._value('vebus', '/BatteryOperationalLimits/MaxChargeCurrent', now)),
                'quattro_power_w': self._quattro_dc_w(now),
                'quattro_reported_power_w': finite(self._value('vebus', '/Dc/0/Power', now)), 'solar': solar}

    def _sources_coherent(self, now, stamp, *, support_only=False, trial=False):
        """Support needs fresh held observations; attribution additionally aligns them."""
        paths = [('vebus', '/Dc/0/Voltage'), ('vebus', '/Dc/0/Current'), ('vebus', '/Ac/ActiveIn/ActiveInput'),
                 ('vebus', '/BatteryOperationalLimits/MaxChargeVoltage'),
                 ('vebus', '/BatteryOperationalLimits/MaxChargeCurrent'),
                 ('system', '/Ac/Consumption/L1/Power'), ('system', '/Dc/System/Power')]
        for instance in (() if support_only else self.driver.cfg.policy_mppt_instances):
            paths.extend(('solar%d' % instance, path) for path in
                ('/Yield/Power', '/Dc/0/Current', '/Dc/0/Voltage', '/Link/ChargeVoltage', '/Link/ChargeCurrent'))
            role = 'solar%d' % instance
            active = self._value(role, '/Link/VoltageSenseActive', now)
            if self._sense_required(role):
                if active not in (0, 1):
                    return False
                paths.append((role, '/Link/VoltageSenseActive'))
            if active == 1:
                paths.append((role, '/Link/VoltageSense'))
        if stamp is None:
            return False
        timestamps = [stamp]
        for role, path in paths:
            name = self.sources_names.get(role)
            if self.sources.get(name, path, now) is None:
                return False
            timestamps.append(self.sources.samples[(name, path)]['verified'])
        # Root D-Bus replies are asynchronous. Their receipt times are not
        # physical measurement times. Slow support correction may use each
        # independently fresh held value; exact solar attribution stays strict.
        alignment = self.config.source_gap_s if support_only or trial else self.config.source_alignment_s
        if max(timestamps) - min(timestamps) > alignment:
            return False
        return True

    def _solar_attribution(self, now, stamp, connected, actuators, eligible, voltage, current, pv_w):
        """Qualify actual solar storage independently of discretionary movement.

        On shore, changing commands or a charging transient make attribution
        ambiguous. A continuously observed inverter with no active AC input
        cannot supply shore charging; its PV current allocation may still vary.
        """
        continuous = (self.attribution_last_now is not None and
                      0 <= now - self.attribution_last_now <= self.config.source_gap_s)
        self.attribution_last_now = now
        if not continuous:
            self.attribution_island_since = None
        actuators['coherent'] = self._sources_coherent(now, stamp)
        actuators['support_coherent'] = self._sources_coherent(now, stamp, support_only=True)
        response = self.control.get('actuator', {})
        tracking = (response.get('solar_tracking') is True and
                    response.get('regulation_authority') == 'CURRENT' and
                    response.get('mppt_response_ready') is True)
        # Coordinated headroom follows an advancing battery. Its solar voltage
        # changes do not identify a new shore-charging event when fresh physical
        # MPPT proof and unchanged Quattro/current authority remain established.
        command_keys = ('quattro_v', 'ccl_a') if tracking else ('quattro_v', 'solar_v', 'ccl_a')
        observation = (connected, tracking, tuple(self.control.get(key) for key in command_keys),
            actuators['quattro_v'], actuators['quattro_current_limit_a'], tuple(
                (item['current_limit_a'],) if tracking else
                (item['voltage_limit_v'], item['current_limit_a']) for item in actuators['solar']))
        unchanged = self.attribution_observation == observation
        self.attribution_observation = observation
        power = actuators['quattro_power_w']
        valid = (eligible and actuators['coherent'] and connected is not None and
                 power is not None and bool(actuators['solar']) and
                 all(item['valid'] for item in actuators['solar']))
        inverting = (valid and connected is False and power <= 0 and
                     self._value('vebus', '/Ac/ActiveIn/ActiveInput', now) == 240)
        if inverting:
            if self.attribution_island_since is None:
                self.attribution_island_since = now
        else:
            self.attribution_island_since = None
        island_proven = (inverting and
                         now - self.attribution_island_since >= self.config.current_settle_s)
        shore_proven = (connected is True and unchanged and
                        self.control.get('actuator', {}).get('settled', False) and
                        power is not None and power <= self.config.positive_reserve_w + 10)
        if not valid or not (island_proven or shore_proven):
            return None
        return min(pv_w, max(0.0, voltage * current - max(0.0, power)))

    def _relay(self, command, now, wall):
        if command is None:
            return
        name = self.sources_names.get('vebus') or self.driver._find_vebus()
        if not name:
            self.transfer.command_result(command, -1, now, wall)
            self.ledger.save()
            return
        # Reservations and departure history are durable before issuing control.
        if command == 1 and not self.ledger.save():
            self.transfer.command_result(command, -1, now, wall)
            return
        path = '/Ac/Control/IgnoreAcIn%d' % getattr(self.driver.cfg, 'policy_ac_input', 1)
        try:
            code = self.driver.sbus.call_blocking(name, path, BUSITEM, 'SetValue', 'v', [command], timeout=2)
        except Exception:
            code = -1
        self.transfer.command_result(command, code, now, wall)
        # Persist observed refusal/timeout lockout before a restart can erase it.
        self.ledger.save()

    def _update_load_service(self, now, pv_w, battery_power_w, eligible):
        """Permit brief measured PV pulses without carrying proof across gaps."""
        gap = self.pv_activity_last is not None and not 0 <= now - self.pv_activity_last <= self.config.source_gap_s
        pv = finite(pv_w)
        if not eligible or pv is None or pv < 0 or gap:
            self.pv_activity = TimedMean(self.config.stable_admission_s)
            self.pv_activity_last = None
            self.load_service.reset()
            if not eligible or pv is None or pv < 0:
                return False, False
        self.pv_activity_last = now
        useful = self.pv_activity.update(now, pv) >= self.config.minimum_useful_pv_w
        return useful, self.load_service.update(now, battery_power_w, useful, False)

    def prepare_intents(self, target):
        """Apply only leased engine intents before REC computes this tick's limits."""
        if not self.contract.owned:
            return
        active = self.contract.active(self.clock.monotonic(), target)
        request = self.contract.request or {}
        limits = request.get('requested_limits', {}) if active else {}
        # Lost authority returns to shore and retains an existing floor/ceiling
        # reference; it must not suddenly restore slider charging while returning.
        sustain = limits.get('sustain', self.driver.sustain.get('mode', 0)
                             if self.driver.sustain.get('active') else 0)
        if not active and not self.driver.sustain.get('active'):
            previous = self.contract.state.get('policy', {})
            if previous.get('target_soc') == target:
                feed = self._value('vebus', '/Ac/ActiveIn/ActiveInput', self.clock.monotonic())
                if previous.get('mode') == 'DISCHARGE':
                    sustain = 2
                elif (previous.get('mode') == 'CHARGE' and
                      feed == self.driver.cfg.policy_ac_input - 1):
                    # Reconstruct the floor only on freshly confirmed shore.
                    # The first exact charger readback still gates current.
                    sustain = 1
        self.driver._set_sustain(sustain)
        if not active:
            if self.driver.boost.get('active'):
                self.driver._set_boost(0.0)
        elif 'boost_v' in limits and request['request_id'] != self.last_boost_id:
            self.driver._set_boost(limits['boost_v'])
            self.last_boost_id = request['request_id']

    def tick(self, raw_valid, target, soc, voltage, current, target_voltage, safe_voltage, ccl):
        now, wall = self.clock.monotonic(), self.clock.time()
        self._poll(now)
        own_name = 'com.victronenergy.battery.' + self.driver.cfg.batt_suffix
        selected = selected_battery_matches(
            own_name, self.driver.batt_instance,
            self._value('system', '/ActiveBatteryService', now),
            self._value('system', '/ActiveBmsService', now),
            self._value('system', '/Dc/Battery/BatteryService', now),
            self._value('system', '/ActiveBmsInstance', now))
        feed = self._value('vebus', '/Ac/ActiveIn/ActiveInput', now)
        connected = None if feed is None else feed == getattr(self.driver.cfg, 'policy_ac_input', 1) - 1
        self.transfer.observe(connected, now, wall)
        array_power = []
        for instance in getattr(self.driver.cfg, 'policy_mppt_instances', (278, 279)):
            role = 'solar%d' % instance
            power = finite(self._value(role, '/Yield/Power', now))
            if self._value(role, '/Connected', now) != 1 or self._value(role, '/MppOperationMode', now) is None:
                power = None
            array_power.append(power)
        pv_w = sum(array_power) if array_power and all(p is not None for p in array_power) else None
        inverter_power = self._quattro_dc_w(now)
        demand = self.demand_model.estimate(
            self._value('system', '/Ac/Consumption/L1/Power', now),
            self._value('system', '/Dc/System/Power', now),
            measured_dc=self._value('system', '/Dc/System/MeasurementType', now) == 1,
            connected=connected is not False,
            inverter_dc_w=-inverter_power if inverter_power is not None else None)
        measurement_valid = self.driver._health()['Measurements']['valid']
        stamp = self.driver.bms.get('_received', {}).get('Measurements')
        actuators = self._actuator_inputs(now)
        solar_surplus = self._solar_attribution(now, stamp, connected, actuators,
            raw_valid and selected and ccl > 0 and demand.get('valid') and pv_w is not None,
            voltage, current, pv_w)
        actuators['support_coherent'] = bool(actuators['support_coherent'] and raw_valid and selected
                                             and connected is not None)
        actuators['solar_attribution_valid'] = solar_surplus is not None and solar_surplus > 0
        interval = {'charge_wh': 0.0, 'discharge_wh': 0.0}
        if measurement_valid and stamp is not None and stamp != self.last_measurement_stamp:
            if self.measurement_was_valid is False:
                self.ledger.mark_gap('battery_measurement_recovered')
            interval = self.ledger.sample(
                stamp, wall, self.driver.bms['current'], self.driver.bms['voltage'], valid=True,
                soc=soc if raw_valid else None,
                overhead_category=self.control.get('overhead_category'),
                solar_surplus_w=solar_surplus, advance_charge_reference=solar_surplus is not None)
            self.last_measurement_stamp = stamp
        elif not measurement_valid and self.measurement_was_valid is True:
            self.ledger.mark_gap('battery_measurement_invalid')
        self.measurement_was_valid = measurement_valid
        self.ledger.observe_time(now, wall)
        request = self.contract.request or {}
        lease = self.contract.active(now, target_soc=target)
        mode = request.get('mode', 'OFF') if lease else self.control.get('mode', 'OFF')
        self.ledger.set_policy(mode, target, soc=soc if raw_valid else None)
        voltage_ready = bool(self.driver.voltage_control.ready)
        commands = self.driver.voltage_control
        qv = commands.requested_quattro
        sv = commands.requested_solar
        control = dict(mode=mode, quattro_v=qv, solar_v=sv, ccl_a=ccl,
                       protect=False, limited_by='', overhead_category=None,
                       actuator={'settled': voltage_ready})
        self.control = control
        actual_load_service, load_service_proven = self._update_load_service(
            now, pv_w, voltage * current,
            raw_valid and selected and connected is False and demand.get('valid') and actuators['coherent'])
        self.last_connected = connected
        discharge_permitted = (raw_valid and self.driver.bms.get('dcl', 0) > 0 and
            not self.driver.bms.get('modulesBlockingDischarge') and not self.driver.bms.get('modulesOffline'))
        source_valid = bool(raw_valid and selected and connected is not None and
                            actuators['support_coherent'] and demand.get('valid'))
        safe = bool(source_valid and self.driver._voltage_within_envelope(safe_voltage))
        current_limit = finite(self._value('vebus', '/BatteryOperationalLimits/MaxChargeCurrent', now))
        current_envelope = ccl + (demand.get('external_dc_w', 0) / max(voltage, 1)
                                 if demand.get('measured_dc') else 0)
        current_ready = current_limit is not None and 0 <= current_limit <= current_envelope + .5
        ready = bool(safe and voltage_ready and current_ready)
        permitted = bool(safe and discharge_permitted and lease and mode != 'OFF')
        departure_allowed = bool(ready and permitted and not self.transfer.departure_reason(now, wall))
        if self.contract.owned:
            intent = request.get('transfer_intent', 'protect') if lease else 'protect'
            protective = not permitted
            # Exact command readbacks gate new departures. Once islanded,
            # changed setpoints and missing MPPT telemetry do not fabricate a
            # battery deficit; the engine owns its observed-power decision.
            transfer_ready = safe if connected is False else ready
            command = self.transfer.step(intent, now, wall, ready=transfer_ready,
                                         permitted=permitted, protective=protective)
            self._relay(command, now, wall)
            control['protect'] = protective
            control['limited_by'] = ('lease expired' if not lease else
                'critical sources or REC discharge permission unavailable' if not permitted else
                self.transfer.limited_by)
        status = self.contract.status(now)
        status.update(mode=control['mode'], ready=ready, lease_valid=lease,
                      source_valid=source_valid, departure_allowed=departure_allowed,
                      request_current=lease, sources_coherent=actuators['coherent'],
                      departure_available=bool(connected is True and not self.transfer.departure_reason(now, wall)),
                      actuator_settled=bool(control.get('actuator', {}).get('settled', False)), limits={k: control[k] for k in ('quattro_v', 'solar_v', 'ccl_a')},
                      transfer=self.transfer.snapshot(now, wall), limited_by=control['limited_by'])
        ledger = self.ledger.snapshot(compact=True)
        public_ledger = {k: ledger[k] for k in ('total', 'overhead', 'references', 'budget', 'reverse', 'buffer',
                                               'net_wh', 'net_ah', 'gap_count', 'complete_history', 'capacity_version', 'calendar_days', 'recovery')}
        snapshot = {'version': 2, 'implementation_version': '3.0.0',
                    'configuration_id': self.configuration_id, 'shore_ac_input': self.driver.cfg.policy_ac_input,
                    'wall_s': wall, 'sample_monotonic_s': stamp,
                    'support_coherent': actuators['support_coherent'],
                    'source_poll': self.poll_diagnostics,
                    'source_valid': source_valid, 'valid': bool(raw_valid), 'soc': soc if raw_valid else None,
                    'voltage': voltage if raw_valid else None, 'current': current if raw_valid else None,
                    'control': control, 'demand': demand, 'ledger': public_ledger}
        self.driver._pub[PREFIX + 'Status'] = dumps(status)
        self.driver._pub[PREFIX + 'Snapshot'] = dumps(snapshot)
        self.driver._pub[PREFIX + 'Owned'] = int(self.contract.owned)
        solar_evidence = {'observed_w': pv_w, 'capacity_lower_bound_w': pv_w,
                          'load_service_verified': load_service_proven,
                          'confidence': ('unavailable' if pv_w is None else
                              'load service verified; headroom unknown' if load_service_proven else 'lower bound'),
                          'required_dc_w': demand.get('admission_w')}
        self.telemetry.publish(self.driver._pub, status, snapshot, ledger=ledger,
            solar_evidence=solar_evidence, sources=self.sources, source_names=self.sources_names, now=now)
        self.last_snapshot = snapshot
        return control

    def shutdown(self):
        if self.contract.owned:
            self._relay(0, self.clock.monotonic(), self.clock.time())
        self.ledger.save()
