"""REC's policy boundary, durable accounting and sole protocol relay writer."""
import logging
import os
import hashlib
import time

from control_inputs import DemandModel, SourceRegistry, LoadServiceEvidence, TimedMean, finite, normalize_bus_snapshot, selected_battery_matches
from energy_accounting import EnergyLedger
from policy_contract import PolicyContract, TransferSupervisor, dumps, resolve_shore_input, prefer_renewable_wanted
from rec_control_config import ControlConfig
from policy_telemetry import PolicyTelemetry

log = logging.getLogger('dbus-recbms')
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
        self.contract = PolicyContract(self.ledger.controller_state.setdefault('contract', {}),
                                       max_boost_v=getattr(cfg, 'boost_max_v', 0.30))
        self.transfer = TransferSupervisor(self.ledger.controller_state.setdefault('transfer', {}),
            connected_dwell_s=self.config.connected_dwell_s, timeout_s=self.config.transfer_timeout_s,
            hourly_departures=int(self.config.hourly_departures), daily_departures=int(self.config.daily_departures),
            backoff_s=self.config.failed_probe_backoff_s, prepare_s=self.config.return_prepare_s)
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
        # 3.4.0: the shore AC input is resolved, not configured (owner,
        # 2026-09-15: shore moves from AC in 1 to AC in 2). Restored from the
        # durable transfer state so a restart on an island keeps ignoring
        # the input it left.
        durable_input = self.transfer.durable.get('shore_input')
        self.shore_input = durable_input if durable_input in (1, 2) else None
        self.shore_input_reason = 'restored' if self.shore_input else ''
        self.gx_input_types = (None, None)
        self.gx_types_at = None
        # 3.5.0: the Quattro's "Prefer renewable energy" toggle, managed
        # daily. daylight: True/False/None (not yet known).
        self.daylight = None
        self.light_since = None
        self.dark_since = None
        self.prefer_wanted = None
        self.prefer_last = None
        self.prefer_reason = ''
        self.prefer_written_at = None
        self.discovery_at = None
        self.discovery_names = None
        self.read_issued = {}
        self.read_applied = {}
        self.read_pending = {}
        self.poll_diagnostics = {}
        self.last_measurement_stamp = None
        self.measurement_was_valid = None
        self.control = {}
        self.settle_since = None
        self.settle_last = None
        self.unpermitted_since = None
        self.attribution_observation = None
        self.attribution_island_since = None
        self.attribution_last_now = None
        self.island_folded = False      # 3.7.4: the servo fold at the island edge, once per island
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
        if accepted and (self.contract.request or {}).get(
                'requested_limits', {}).get('purpose') == 'failed_probe':
            self._failed_probe(now)
        driver.batt[PREFIX + 'Owned'] = int(self.contract.owned)
        return accepted

    def _failed_probe(self, now):
        """Feed one evaluated probe failure into the durable backoff, once.

        D16/issue #8: `TransferSupervisor.failed_probe` had no caller, so the
        protection lived only in the engine and a restart erased it. The
        consumer repeats the marker until it sees its request id accepted and
        renumbers requests across its own restarts, so the DEPARTURE the
        failure belongs to is what is counted -- the physically confirmed one,
        never a refused or timed-out attempt that never left shore. The ledger
        is saved immediately so a restart cannot erase the backoff either.
        """
        durable = self.transfer.durable
        departure = durable.get('last_departure_s')
        if departure is None or durable.get('last_failed_departure') == departure:
            return
        durable['last_failed_departure'] = departure
        self.transfer.failed_probe(self.clock.time(), now)
        self.ledger.save()

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
                         '/Connected', '/Ac/ActiveIn/ActiveInput', '/Ac/ActiveIn/Connected',
                         '/Ac/State/AcIn1Available', '/Ac/State/AcIn2Available',
                         '/Ac/State/IgnoreAcIn1', '/Ac/State/IgnoreAcIn2',
                         '/Dc/0/PreferRenewableEnergy',
                         '/Ac/Out/L1/P', '/Dc/0/Power', '/Dc/0/Current',
                         '/Dc/0/Voltage', '/BatteryOperationalLimits/MaxChargeCurrent',
                         '/BatteryOperationalLimits/MaxChargeVoltage', '/Dc/0/MaxChargeCurrent')
                if source_role.startswith('solar'):
                    paths = ('/Yield/Power', '/MppOperationMode', '/Connected', '/Dc/0/Current', '/Pv/V',
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

    def _gx_input_types(self, now):
        """The GX's AC input types, read from localsettings every 30 s
        (two blocking GetValues; the whole settings tree is far too big to
        poll like the other sources). None when unreadable."""
        if self.gx_types_at is None or now - self.gx_types_at >= 30:
            self.gx_types_at = now
            types = []
            for n in (1, 2):
                value = self.driver._settings_get('/Settings/SystemSetup/AcInput%d' % n)
                try:
                    types.append(int(value) if value is not None else None)
                except (TypeError, ValueError):
                    types.append(None)
            self.gx_input_types = tuple(types)
        return self.gx_input_types

    def _resolve_shore_input(self, now):
        """Which AC input the REC controls (policy_contract.resolve_shore_input).
        On a change the input being left is released: a standing ignore on
        it would otherwise outlive the switch with nobody left to clear it."""
        configured = getattr(self.driver.cfg, 'policy_ac_input', 'auto')
        feed = self._value('vebus', '/Ac/ActiveIn/ActiveInput', now)
        available = (self._value('vebus', '/Ac/State/AcIn1Available', now),
                     self._value('vebus', '/Ac/State/AcIn2Available', now))
        new, reason = resolve_shore_input(configured, self._gx_input_types(now), feed, available,
                                          self.shore_input)
        # The fallback is provisional: it is used this tick but never
        # settled, so the first ticks before the Quattro's values arrive
        # cannot pin input 1 for good (fixture, 2026-09-15).
        if reason != 'default' and new != self.shore_input:
            old = self.shore_input
            log.info('shore AC input %s -> %d (%s; GX types %s, ActiveInput %s, available %s)',
                     old, new, reason, self.gx_input_types, feed, available)
            if old in (1, 2):
                self._release_input(old, now)
            self.shore_input = new
            self.transfer.durable['shore_input'] = new
            self.ledger.save()
        self.shore_input_reason = reason
        return new

    def _daylight(self, now):
        """True once the brightest array's voltage has been at least
        dawn_voc_v for dawn_s, False once every array has read under
        dusk_voc_v for dusk_s, None until either has happened. The ARRAY
        voltage, never the PV current: on shore the hold's cap curtails the
        current to a tenth of an amp under a bright sky (2026-09-15), which a
        current-based rule would have called night at two in the afternoon.
        Two timers with a gap between their thresholds, so a passing cloud
        changes nothing and each edge comes once a day."""
        cfg = self.driver.cfg
        volts = []
        for instance in getattr(cfg, 'policy_mppt_instances', (278, 279)):
            v = finite(self._value('solar%d' % instance, '/Pv/V', now))
            if v is not None:
                volts.append(v)
        brightest = max(volts) if volts else None
        if brightest is None:
            self.light_since = self.dark_since = None
        elif brightest >= cfg.policy_dawn_voc_v:
            self.dark_since = None
            self.light_since = now if self.light_since is None else self.light_since
            if now - self.light_since >= cfg.policy_dawn_s:
                self.daylight = True
        elif brightest < cfg.policy_dusk_voc_v:
            self.light_since = None
            self.dark_since = now if self.dark_since is None else self.dark_since
            if now - self.dark_since >= cfg.sustain_dusk_s:
                self.daylight = False
        return self.daylight

    def _prefer_renewable(self, now, mode, soc, target):
        """Own the Quattro's "Prefer renewable energy" toggle the way the
        relay is owned: decide (policy_contract.prefer_renewable_wanted),
        compare with the read-back value, write only on a difference and at
        most once a minute, and publish wanted / actual / reason."""
        cfg = self.driver.cfg
        actual = self._value('vebus', '/Dc/0/PreferRenewableEnergy', now)
        actual = int(actual) if actual in (0, 1) else None
        if getattr(cfg, 'policy_prefer_renewable', 'auto') != 'auto':
            self.prefer_wanted, self.prefer_reason = None, 'not managed (policy prefer_renewable = off)'
            return actual
        hold = self.driver.sustain
        reference = target
        if mode == 'CHARGE' and hold.get('active') and hold.get('mode') == 1 and hold.get('soc') is not None:
            reference = hold['soc']
        wanted, reason = prefer_renewable_wanted(mode, self._daylight(now), soc, reference,
                                                 cfg.policy_day_deficit_pct, self.prefer_last)
        self.prefer_wanted, self.prefer_reason = wanted, reason
        if wanted is None:
            return actual
        self.prefer_last = wanted
        name = self.sources_names.get('vebus')
        if actual is None or not name:
            self.prefer_reason = reason + ' (toggle not readable)'
            return actual
        if actual != wanted and (self.prefer_written_at is None or now - self.prefer_written_at >= 60):
            self.prefer_written_at = now
            try:
                code = self.driver.sbus.call_blocking(name, '/Dc/0/PreferRenewableEnergy', BUSITEM,
                                                      'SetValue', 'v', [wanted], timeout=2)
            except Exception as exc:
                code = exc
            log.info('prefer renewable energy %d -> %d (%s; write %s)', actual, wanted, reason, code)
        return actual

    def _release_input(self, number, now):
        state = self._value('vebus', '/Ac/State/IgnoreAcIn%d' % number, now)
        name = self.sources_names.get('vebus')
        if state != 1 or not name:
            return
        try:
            code = self.driver.sbus.call_blocking(name, '/Ac/Control/IgnoreAcIn%d' % number, BUSITEM,
                                                  'SetValue', 'v', [0], timeout=2)
        except Exception as exc:
            code = exc
        log.warning('released the ignore standing on AC in %d after the shore input moved (%s)', number, code)

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
        # Command readiness, not the stricter physical settling added in stage
        # A: attribution already requires an unchanged observation and a
        # Quattro inside the positive reserve, so demanding current_settle_s
        # of continuity here as well would only delay credit that the previous
        # behaviour granted.
        shore_proven = (connected is True and unchanged and
                        self.control.get('actuator', {}).get('command_ready', False) and
                        power is not None and power <= self.config.positive_reserve_w + 10)
        if not valid or not (island_proven or shore_proven):
            return None
        return min(pv_w, max(0.0, voltage * current - max(0.0, power)))

    def _relay(self, command, now, wall):
        if command is None:
            return
        name = self.sources_names.get('vebus') or self.driver._find_vebus()
        if not name:
            # No VE.Bus service to write to (a restart): nothing was refused,
            # so no fault -- the command is re-asserted when it is back.
            self.transfer.command_deferred(command, now)
            return
        # Reservations and departure history are durable before issuing control.
        if command == 1 and not self.ledger.save():
            self.transfer.command_result(command, -1, now, wall)
            return
        path = '/Ac/Control/IgnoreAcIn%d' % (self.shore_input or 1)
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

    def _actuator_state(self, now, command_ready, connected, actuators, dc_load_w=0.0):
        """A3/D04: command readiness is not charger settling.

        ``command_ready`` is the acknowledgment of the exact requested pair and
        a current readback inside the envelope -- the gate on a transfer.
        ``settled`` is the physical response: signed Quattro V*I (never the
        reported /Dc/0/Power, which held a 25-26 W residual at 0.0 A on
        2026-09-12) continuously inside the positive reserve for
        current_settle_s while actually on shore. On shore the Quattro's DC
        output also carries the DC loads (about 50 W on this boat, 90 W
        read 2026-09-14 05:39 UTC under a settled floor), so the reserve is
        judged over and above the measured DC demand. Publishing a command
        is not evidence that the hardware has applied it.
        """
        allowance = self.config.positive_reserve_w + 10 + max(0.0, finite(dc_load_w) or 0.0)
        quiet = bool(connected is True and command_ready and
                     actuators['quattro_power_w'] is not None and
                     actuators['quattro_power_w'] <= allowance)
        continuous = (self.settle_last is not None and
                      0 <= now - self.settle_last <= self.config.source_gap_s)
        self.settle_last = now
        if not quiet or not continuous:
            self.settle_since = now if quiet else None
        elif self.settle_since is None:
            self.settle_since = now
        settled = bool(quiet and self.settle_since is not None and
                       now - self.settle_since >= self.config.current_settle_s)
        transfer = self.transfer
        if connected is None:
            state = 'unknown'
        elif connected is False:
            state = ('returning' if transfer.pending == 0 else
                     'preparing' if transfer.state == 'PREPARE_CONNECT' else 'islanded')
        else:
            state = 'connected' if settled else 'settling'
        return {'command_ready': command_ready, 'settled': settled, 'state': state,
                'command_acknowledged': command_ready,
                'transition_age_s': (None if transfer.last_edge_at is None else
                                     max(0.0, now - transfer.last_edge_at))}

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
        # The REC returning on its own -- a lost lease, or sources it can no
        # longer judge under a lease that still says 'island' (E02 on the
        # island: the Quattro's DC current) -- carries the mode's protection
        # itself rather than the island's release: the engine's floor request
        # arrives a tick later at best, and never when the fault is one only
        # the REC can see. `protect` is last tick's verdict; the bounded
        # preparation wait in tick() covers that tick.
        rec_returning = not active or bool(getattr(self, 'control', {}).get('protect'))
        if rec_returning and not (self.driver.sustain.get('active') and sustain):
            policy = (request.get('mode') if active else
                      self.contract.state.get('policy', {}).get('mode'))
            same_target = (active or
                           self.contract.state.get('policy', {}).get('target_soc') == target)
            if same_target:
                feed = self._value('vebus', '/Ac/ActiveIn/ActiveInput', self.clock.monotonic())
                transfer = getattr(self, 'transfer', None)
                # SP65/D02: the floor belongs BEFORE the re-accept, not after
                # it. Reconstruct it as soon as the return begins from a
                # confirmed island, and keep it once any input is accepted --
                # another accepted AC input is a charger too. A known-absent
                # shore changes nothing that is already in force but asks for
                # nothing new, so an attempted return cannot pin solar
                # charging indefinitely.
                accepted = feed in (0, 1)
                returning = (getattr(transfer, 'feedback', None) is False and
                             getattr(transfer, 'available', None) is not False and
                             getattr(transfer, 'limited_by', '') != 'shore unavailable')
                if policy == 'DISCHARGE':
                    sustain = 2
                elif policy == 'OFF' and returning and not accepted:
                    # Switched off while islanded: nothing to hold for, but
                    # the relay may not close on the slider CVL either. A
                    # floor at the present bank carries the return; the OFF
                    # rule below releases it once an input is accepted.
                    sustain = 1
                elif policy in ('CHARGE', 'HOLD') and (accepted or returning):
                    # The first exact charger readback still gates current.
                    # A HOLD reconstructs the two-sided hold (3) exactly as
                    # CHARGE reconstructs its floor: without it the slider
                    # curve comes back with the standing lead under it and
                    # the Quattro sits below the bank (E04/D03).
                    sustain = 1 if policy == 'CHARGE' else 3
        # Stage C (master D13), the operator's side of a lost owner. The
        # four outcomes, each through this same path and the ordinary
        # prepared return, never a new override workflow:
        #  - lease lost: the mode's hold is kept (reconstructed above) at
        #    the target it was taken for, and the REC returns to shore under
        #    it for as long as the loss lasts;
        #  - the target changes during the loss: the retained hold was taken
        #    for another destination and is stale -- once the bank is back
        #    on an accepted input it is released and the slider's own curve
        #    applies, which is what the slider means with no Solar Priority
        #    running; a return still in progress keeps it until then;
        #  - OFF: the consumer's OFF request must carry release (the
        #    contract), but a hold is kept through the protected return so
        #    the relay does not close on the slider CVL, and released once
        #    an input is accepted -- Solar Priority relinquishes the bank
        #    after the return, not during it;
        #  - recovery: a fresh valid request resumes; nothing is remembered
        #    from the loss beyond the ledger's references.
        hold = self.driver.sustain
        transfer = getattr(self, 'transfer', None)
        on_ac = getattr(transfer, 'feedback', None) is True
        stale = (not active and hold.get('active') and
                 self.contract.state.get('policy', {}).get('target_soc') != target)
        if stale and on_ac and sustain:
            log.info('sustain %s released: the target moved to %.0f%% during the lost lease; '
                     'the slider applies', hold.get('mode'), target)
            sustain = 0
        elif (active and request.get('mode') == 'OFF' and hold.get('active') and
              not on_ac and getattr(transfer, 'feedback', None) is not None):
            sustain = hold.get('mode', 0)
        self.driver._set_sustain(sustain)
        if not active:
            if self.driver.boost.get('active'):
                self.driver._set_boost(0.0)
        elif 'boost_v' in limits and request['request_id'] != self.last_boost_id:
            self.driver._set_boost(limits['boost_v'])
            self.last_boost_id = request['request_id']

    def tick(self, raw_valid, target, soc, voltage, current, safe_voltage, ccl):
        now, wall = self.clock.monotonic(), self.clock.time()
        self._poll(now)
        own_name = 'com.victronenergy.battery.' + self.driver.cfg.batt_suffix
        selected = selected_battery_matches(
            own_name, self.driver.batt_instance,
            self._value('system', '/ActiveBatteryService', now),
            self._value('system', '/ActiveBmsService', now),
            self._value('system', '/Dc/Battery/BatteryService', now),
            self._value('system', '/ActiveBmsInstance', now))
        shore_input = self._resolve_shore_input(now)
        feed = self._value('vebus', '/Ac/ActiveIn/ActiveInput', now)
        connected = None if feed is None else feed == shore_input - 1
        # A2/SP56, read on the boat 2026-09-14 (vebus 276): availability, the
        # Quattro's acknowledgment of the ignore command and the accepted
        # input are three separate facts. A missing or stale availability path
        # stays unknown -- never "absent". ActiveInput 240 means no input is
        # accepted; 0 and 1 are AC in 1 and AC in 2.
        available = self._value('vebus', '/Ac/State/AcIn%dAvailable' % shore_input, now)
        available = True if available == 1 else False if available == 0 else None
        ignore_state = self._value('vebus', '/Ac/State/IgnoreAcIn%d' % shore_input, now)
        ignore_state = int(ignore_state) if ignore_state in (0, 1) else None
        active_input = int(feed) if feed in (0, 1) else None
        self.transfer.observe(connected, now, wall, available=available,
                              ignore_state=ignore_state, active_input=active_input)
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
            # Any accepted input is a charger: only ActiveInput 240 (or an
            # unreadable input) leaves the Quattro inverting.
            connected=active_input is not None or feed is None,
            inverter_dc_w=-inverter_power if inverter_power is not None else None)
        measurement_valid = self.driver._health()['Measurements']['valid']
        stamp = self.driver.bms.get('_received', {}).get('Measurements')
        actuators = self._actuator_inputs(now)
        solar_surplus = self._solar_attribution(now, stamp, connected, actuators,
            raw_valid and selected and ccl > 0 and demand.get('valid') and pv_w is not None,
            voltage, current, pv_w)
        actuators['support_coherent'] = bool(actuators['support_coherent'] and raw_valid and selected
                                             and connected is not None)
        if measurement_valid and stamp is not None and stamp != self.last_measurement_stamp:
            if self.measurement_was_valid is False:
                self.ledger.mark_gap('battery_measurement_recovered')
            self.ledger.sample(
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
                       actuator={})
        self.control = control
        prefer_actual = self._prefer_renewable(now, mode, soc if raw_valid else None, target)
        prefer = {'wanted': self.prefer_wanted, 'actual': prefer_actual,
                  'reason': self.prefer_reason, 'daylight': self.daylight}
        _, load_service_proven = self._update_load_service(
            now, pv_w, voltage * current,
            raw_valid and selected and connected is False and demand.get('valid') and actuators['coherent'])
        discharge_permitted = (raw_valid and self.driver.bms.get('dcl', 0) > 0 and
            not self.driver.bms.get('modulesBlockingDischarge') and not self.driver.bms.get('modulesOffline'))
        source_valid = bool(raw_valid and selected and connected is not None and
                            actuators['support_coherent'] and demand.get('valid'))
        envelope_ok = bool(self.driver._voltage_within_envelope(safe_voltage))
        safe = bool(source_valid and envelope_ok)
        current_limit = finite(self._value('vebus', '/BatteryOperationalLimits/MaxChargeCurrent', now))
        current_envelope = ccl + (demand.get('external_dc_w', 0) / max(voltage, 1)
                                 if demand.get('measured_dc') else 0)
        current_ready = current_limit is not None and 0 <= current_limit <= current_envelope + .5
        ready = bool(safe and voltage_ready and current_ready)
        permitted = bool(safe and discharge_permitted and lease and mode != 'OFF')
        departure_allowed = bool(ready and permitted and not self.transfer.departure_reason(now, wall))
        control['actuator'] = self._actuator_state(now, ready, connected, actuators,
                                                   demand.get('external_dc_w', 0.0))
        if self.contract.owned:
            intent = request.get('transfer_intent', 'protect') if lease else 'protect'
            protective = not permitted
            # A1/A3 (D02): every return that can afford it goes through the
            # bounded preparation wait in TransferSupervisor.step, so the
            # below-bank pair is verified before the relay closes. A lost
            # lease, OFF and lost transport sources (E02: the Quattro's DC
            # current) are not urgent -- the loads are on the bank either
            # way -- so they return as a 'connected' intent too. Only what
            # the REC itself can no longer vouch for is urgent and returns
            # at once: critical REC data lost, discharge prohibited by the
            # BMS, or a charger outside the safe envelope.
            urgent = not (raw_valid and discharge_permitted and envelope_ok)
            transient = protective and not urgent and mode != 'OFF'
            if transient and self.unpermitted_since is None:
                self.unpermitted_since = now
            elif not transient:
                self.unpermitted_since = None
            if transient and now - self.unpermitted_since < self.config.source_gap_s:
                # A source that blinks for a tick is not a reason to move
                # the relay: the engine judges its island on inputs that
                # tolerate 20 s, and a return here is a relay edge plus a
                # charge tail. The lease's own intent stands through the
                # grace; no new departure can start (the island is already
                # there or the lease is what it is), and the electrical
                # envelope is still checked below.
                protective = False
                grace = True
                if intent == 'protect':
                    # No lease to carry an intent through the grace (a
                    # slider move revokes it for the tick or two the
                    # consumer needs to re-request): keep what the bank is
                    # doing. On the boat, 2026-09-14 22:18 UTC, a target
                    # change on an island closed the relay on the old pair
                    # within two seconds because 'protect' bypassed both
                    # the grace and the prepared wait.
                    intent = 'island' if connected is False else 'connected'
            else:
                grace = False
            if protective and not urgent:
                intent, protective = 'connected', False
            boost_cleared = False
            if intent != 'island' and connected is False and self.driver.boost['active']:
                # 3.5.0: a return closes the relay on the prepared pair AND
                # whatever current cap is configured. A solar boost lifts
                # that cap for its whole length (240 s now), and a return
                # that starts inside one would otherwise be "prepared"
                # against a raw limit (fixture: closures at 200 A). The
                # measurement is over the moment the island is being
                # left: clear it here, and hold this tick's readiness back
                # -- the limit the driver computed this tick is still the
                # lifted one -- so the cap is what the readback has to
                # confirm before the relay moves.
                self.driver._boost_clear('return to shore')
                boost_cleared = True
            # Exact command readbacks gate new departures. Once islanded,
            # changed setpoints and missing MPPT telemetry do not fabricate a
            # battery deficit; the engine owns its observed-power decision
            # (SP67), so an island intent still only needs a safe envelope.
            # A return waits for the pair itself -- the REC's own readback of
            # the requested voltages and the current limit -- not for the
            # transport sources that a departure needs.
            # -- and, under any regulating policy, for the hold that makes the
            # pair a protection at all: a CHARGE return is prepared only once
            # its floor is anchored (the engine's request, or the REC's own
            # reconstruction above, one tick later), a DISCHARGE return once
            # its ceiling is, and a HOLD return once its two-sided hold is.
            hold = self.driver.sustain
            hold_mode = hold.get('mode') if hold.get('active') and hold.get('soc') is not None else None
            protected = ((mode != 'CHARGE' or hold_mode == 1) and
                         (mode != 'DISCHARGE' or hold_mode == 2) and
                         (mode != 'HOLD' or hold_mode == 3) and
                         (mode != 'OFF' or connected is not False or hold_mode is not None))
            if connected is False and intent == 'island':
                if not self.island_folded:
                    # 3.7.5: the hold at its destination re-anchors on the
                    # bank as it islands (its present voltage less R * I) and
                    # drops whatever its servo carried for the shore side.
                    self.island_folded = True
                    self.driver._sustain_island_edge(voltage, current, 'islanded: the hold regulates from the bank as it is')
            elif connected:
                self.island_folded = False
            if connected is False and intent == 'island':
                transfer_ready = envelope_ok if grace else safe
            elif intent == 'connected':
                transfer_ready = bool(voltage_ready and current_ready and protected and not boost_cleared)
            else:
                transfer_ready = ready
            command = self.transfer.step(intent, now, wall, ready=transfer_ready,
                                         permitted=permitted or grace, protective=protective)
            self._relay(command, now, wall)
            control['protect'] = not permitted
            control['limited_by'] = ('lease expired' if not lease else
                'critical sources or REC discharge permission unavailable' if not permitted else
                self.transfer.limited_by)
        status = self.contract.status(now)
        status.update(mode=control['mode'], ready=ready, lease_valid=lease,
                      source_valid=source_valid, departure_allowed=departure_allowed,
                      shore_ac_input=self.shore_input, shore_ac_input_reason=self.shore_input_reason,
                      prefer_renewable=prefer,
                      request_current=lease, sources_coherent=actuators['coherent'],
                      departure_available=bool(connected is True and not self.transfer.departure_reason(now, wall)),
                      actuator_settled=bool(control.get('actuator', {}).get('settled', False)), limits={k: control[k] for k in ('quattro_v', 'solar_v', 'ccl_a')},
                      transfer=self.transfer.snapshot(now, wall), limited_by=control['limited_by'])
        ledger = self.ledger.snapshot(compact=True)
        public_ledger = {k: ledger[k] for k in ('total', 'overhead', 'references', 'budget', 'reverse', 'buffer',
                                               'net_wh', 'net_ah', 'gap_count', 'complete_history', 'capacity_version', 'calendar_days', 'recovery')}
        snapshot = {'version': 2, 'implementation_version': '3.5.0',
                    'configuration_id': self.configuration_id, 'shore_ac_input': self.shore_input,
                    'prefer_renewable': prefer,
                    'shore_ac_input_reason': self.shore_input_reason,
                    'shore_ac_input_configured': self.driver.cfg.policy_ac_input,
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
        return control

    def shutdown(self):
        if self.contract.owned:
            self._relay(0, self.clock.monotonic(), self.clock.time())
        self.ledger.save()
