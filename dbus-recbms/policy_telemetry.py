"""Stable scalar policy telemetry derived from the authoritative snapshots.

No accounting, demand estimation or policy decisions occur here. Missing facts
are published as invalid values so an old chart value cannot masquerade as a
current observation. Status/Snapshot JSON remain the coherent full documents.
"""
PREFIX = '/RecBms/Policy/Telemetry/'


# (D-Bus suffix, source dictionary, nested source keys). Units are part of the
# path name wherever a number could otherwise be ambiguous.
FIELDS = (
    ('Sample/WallSeconds', 'snapshot', ('wall_s',)),
    ('Sample/MonotonicSeconds', 'snapshot', ('sample_monotonic_s',)),
    ('Actuator/SupportCoherent', 'snapshot', ('support_coherent',)),
    ('Mode', 'snapshot', ('control', 'mode')),
    ('Transport', 'status', ('transfer', 'state')),
    ('LimitedBy', 'status', ('limited_by',)),
    ('Ready', 'status', ('ready',)),
    ('Protect', 'snapshot', ('control', 'protect')),
    ('Connected', 'status', ('transfer', 'connected')),
    ('LeaseValid', 'status', ('lease_valid',)),
    ('LeaseRemainingSeconds', 'status', ('lease_remaining_s',)),
    ('AcceptedRequestId', 'status', ('accepted_id',)),
    ('RequestRejection', 'status', ('rejection',)),
    ('ConfigurationId', 'snapshot', ('configuration_id',)),
    ('Battery/Valid', 'snapshot', ('valid',)),
    ('Battery/VoltageV', 'snapshot', ('voltage',)),
    ('Battery/CurrentA', 'snapshot', ('current',)),
    ('Battery/SocPct', 'snapshot', ('soc',)),
    ('Limits/QuattroVoltageV', 'status', ('limits', 'quattro_v')),
    ('Limits/SolarVoltageV', 'status', ('limits', 'solar_v')),
    ('Limits/ChargeCurrentA', 'status', ('limits', 'ccl_a')),
    ('Actuator/Settled', 'snapshot', ('control', 'actuator', 'settled')),
    ('Actuator/CommandAcknowledged', 'snapshot', ('control', 'actuator', 'command_acknowledged')),
    ('Actuator/State', 'snapshot', ('control', 'actuator', 'state')),
    ('Actuator/TransitionAgeSeconds', 'snapshot', ('control', 'actuator', 'transition_age_s')),
    ('Actuator/SourcesCoherent', 'status', ('sources_coherent',)),
    ('Relay/FaultReason', 'status', ('transfer', 'last_fault', 'reason')),
    ('Ledger/NetAh', 'ledger', ('net_ah',)),
    ('Ledger/NetWh', 'ledger', ('net_wh',)),
    ('Ledger/CapacityVersion', 'ledger', ('capacity_version',)),
    ('Ledger/CompleteHistory', 'ledger', ('complete_history',)),
    ('Ledger/GapCount', 'ledger', ('gap_count',)),
    ('References/HighAh', 'ledger', ('references', 'high_ah')),
    ('References/LowAh', 'ledger', ('references', 'low_ah')),
    ('References/HighWh', 'ledger', ('references', 'high_wh')),
    ('References/LowWh', 'ledger', ('references', 'low_wh')),
    ('References/HighSocPct', 'ledger', ('references', 'high_soc')),
    ('References/LowSocPct', 'ledger', ('references', 'low_soc')),
    ('References/TargetSocPct', 'ledger', ('references', 'target_soc')),
    ('Buffer/CreditAh', 'ledger', ('buffer', 'credit_ah')),
    ('Buffer/CreditWh', 'ledger', ('buffer', 'credit_wh')),
    ('Recovery/ChargeDebtAh', 'ledger', ('recovery', 'charge_ah')),
    ('Recovery/DischargeDebtAh', 'ledger', ('recovery', 'discharge_ah')),
    ('Budget24h/SpentEfc', 'ledger', ('budget', 'spent_efc')),
    ('Budget24h/ReservedEfc', 'ledger', ('budget', 'reserved_efc')),
    ('Budget24h/RemainingEfc', 'ledger', ('budget', 'remaining_efc')),
    ('Budget24h/SpentWh', 'ledger', ('budget', 'spent_wh')),
    ('Budget24h/ReservedWh', 'ledger', ('budget', 'reserved_wh')),
    ('Budget24h/RemainingWh', 'ledger', ('budget', 'remaining_wh')),
    ('Budget24h/ReverseWh', 'ledger', ('budget', 'reverse_wh')),
    ('Budget24h/ReverseRemainingWh', 'ledger', ('budget', 'reverse_remaining_wh')),
    ('Budget24h/Uncertain', 'ledger', ('budget', 'uncertain')),
    ('Budget24h/Reason', 'ledger', ('budget', 'reason')),
    ('Reverse/EventWh', 'ledger', ('reverse', 'event_wh')),
    ('Reverse/EventRemainingWh', 'ledger', ('reverse', 'event_remaining_wh')),
    ('Reverse/TotalWh', 'ledger', ('reverse', 'total_wh')),
    ('Overhead/Category', 'snapshot', ('control', 'overhead_category')),
    ('Relay/Departures24h', 'status', ('transfer', 'departures_24h')),
    ('Relay/ActualEdges24h', 'status', ('transfer', 'actual_edges_24h')),
    ('Relay/ExternalEdges', 'status', ('transfer', 'external_edges')),
    ('Relay/NextDepartureSeconds', 'status', ('transfer', 'next_departure_s')),
    ('Relay/PendingCommand', 'status', ('transfer', 'pending')),
    ('Relay/LimitedBy', 'status', ('transfer', 'limited_by')),
    ('Demand/Valid', 'snapshot', ('demand', 'valid')),
    ('Demand/Method', 'snapshot', ('demand', 'method')),
    ('Demand/ExternalMeasured', 'snapshot', ('demand', 'measured_dc')),
    ('Demand/UncertaintyW', 'snapshot', ('demand', 'uncertainty_w')),
    ('Demand/IslandW', 'snapshot', ('demand', 'island_w')),
    ('Demand/AdmissionW', 'snapshot', ('demand', 'admission_w')),
    ('Demand/ConnectedDcW', 'snapshot', ('demand', 'connected_dc_w')),
    ('Demand/ExternalDcW', 'snapshot', ('demand', 'external_dc_w')),
    ('Demand/InverterDcW', 'snapshot', ('demand', 'inverter_dc_w')),
    ('Demand/LimitedBy', 'snapshot', ('demand', 'limited_by')),
    ('Solar/ObservedW', 'solar', ('observed_w',)),
    ('Solar/CapacityLowerBoundW', 'solar', ('capacity_lower_bound_w',)),
    ('Solar/LoadServiceVerified', 'solar', ('load_service_verified',)),
    ('Solar/Confidence', 'solar', ('confidence',)),
    ('Solar/RequiredDcW', 'solar', ('required_dc_w',)),
)
TOTAL_FIELDS = tuple(
    ('Ledger/%s/%s' % (label, field), 'ledger', (source, key))
    for label, source in (('Total', 'total'), ('Overhead', 'overhead'))
    for field, key in (('ChargeAh', 'charge_ah'), ('DischargeAh', 'discharge_ah'),
                       ('ChargeWh', 'charge_wh'), ('DischargeWh', 'discharge_wh'), ('Efc', 'efc')))

INPUT_FIELDS = {
    'system': (('AcLoadW', '/Ac/Consumption/L1/Power'), ('DcLoadW', '/Dc/System/Power'),
               ('DcMeasurementType', '/Dc/System/MeasurementType'),
               ('SelectedBattery', '/ActiveBatteryService'), ('SelectedBms', '/ActiveBmsService')),
    'vebus': (('ActiveInput', '/Ac/ActiveIn/ActiveInput'), ('DcPowerW', '/Dc/0/Power'),
              ('ChargeCurrentLimitA', '/BatteryOperationalLimits/MaxChargeCurrent')),
    'solar': (('PowerW', '/Yield/Power'), ('TrackingMode', '/MppOperationMode'),
              ('Connected', '/Connected')),
}


def _lookup(document, keys):
    for key in keys:
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return int(document) if isinstance(document, bool) else document


class PolicyTelemetry:
    """Register once, then publish from each adapter's existing atomic batch."""
    def __init__(self, source_roles=()):
        self.fields = FIELDS + TOTAL_FIELDS
        self.inputs = tuple((role, name, path)
                            for role in source_roles
                            for name, path in INPUT_FIELDS.get('solar' if role.startswith('solar') else role, ()))

    def register(self, service):
        for suffix, _, _ in self.fields:
            service.add_path(PREFIX + suffix, None)
        for role, name, _ in self.inputs:
            for suffix in ('Valid', 'AgeSeconds', 'ChangedAgeSeconds'):
                service.add_path(PREFIX + 'Inputs/%s/%s/%s' % (role, name, suffix), None)
        for role in dict.fromkeys(role for role, _, _ in self.inputs):
            service.add_path(PREFIX + 'Inputs/%s/Service' % role, None)

    def publish(self, service, status, snapshot, ledger=None, solar_evidence=None,
                sources=None, source_names=None, now=None):
        """Publish no new facts: use full ledger for debt and registry for ages.

        ``service`` may be the existing VeDbusService batch context. ``now`` and
        registry receipt times are monotonic seconds. The caller retains JSON
        publication in the same batch; this method never writes those paths.
        """
        documents = {'status': status, 'snapshot': snapshot,
                     'ledger': snapshot.get('ledger', {}) if ledger is None else ledger,
                     'solar': solar_evidence if solar_evidence is not None else snapshot.get('capacity', {})}
        for suffix, document, keys in self.fields:
            value = _lookup(documents[document], keys)
            if suffix == 'LimitedBy' and not value:
                value = _lookup(status, ('transfer', 'limited_by')) or _lookup(snapshot, ('control', 'limited_by')) or ''
            service[PREFIX + suffix] = value
        names = source_names or {}
        for role in dict.fromkeys(role for role, _, _ in self.inputs):
            service[PREFIX + 'Inputs/%s/Service' % role] = names.get(role)
        for role, name, path in self.inputs:
            sample = sources.samples.get((names.get(role), path)) if sources is not None else None
            age = changed_age = None
            valid = None
            if sample is not None and now is not None:
                age = max(0.0, now - sample['verified'])
                changed_age = max(0.0, now - sample['changed'])
                valid = int(sources.get(names.get(role), path, now) is not None)
            prefix = PREFIX + 'Inputs/%s/%s/' % (role, name)
            service[prefix + 'Valid'] = valid
            service[prefix + 'AgeSeconds'] = age
            service[prefix + 'ChangedAgeSeconds'] = changed_age
