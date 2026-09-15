#!/usr/bin/env python3
"""Record a Solar Priority transfer trace from the Cerbo over MQTT (read-only).

Subscribes to the handful of paths that describe a shore departure or return
-- the Quattro's active input, the ignore command and its acknowledged state
on BOTH AC inputs, both availabilities, the GX's two AC input types, the
commanded and accepted voltage pair, the CCL, the sustain hold, the policy
transport state and the signed Quattro V x I -- and writes one JSON line per
second to a file, plus a line on every relay edge. Shore is not nailed to AC
in 1 (the owner rewired it to AC in 2 on 2026-09-15), so each row also
carries the resolved shore_input and the single-input ignore_state,
ignore_cmd and shore_available taken from it.
MQTT on port 1883 is the read path that never disturbs the box (no SSH); the
broker needs a keepalive every 30 s to keep publishing, which this sends.

    python3 solar_watch.py --host 192.168.50.107 --portal 102c6b8611dd \
        --out trace.jsonl [--hours 12] [--vebus 276] [--battery 200]

Summarise a recorded trace (relay edges with the commands in force at the
edge, the Quattro power tail after each closure):

    python3 solar_watch.py --summary trace.jsonl
"""
import argparse
import json
import sys
import time

TOPICS = {
    'vebus': {
        'Ac/ActiveIn/ActiveInput': 'active_input',
        # Both inputs, always: which one is shore is the installation's fact,
        # and a stranded ignore on the other one is exactly what we watch for.
        'Ac/State/IgnoreAcIn1': 'ignore_state1',
        'Ac/State/IgnoreAcIn2': 'ignore_state2',
        'Ac/Control/IgnoreAcIn1': 'ignore_cmd1',
        'Ac/Control/IgnoreAcIn2': 'ignore_cmd2',
        'Ac/State/AcIn1Available': 'ac1_available',
        'Ac/State/AcIn2Available': 'ac2_available',
        'Ac/ActiveIn/Connected': 'ac_connected',
        'Dc/0/Voltage': 'q_v', 'Dc/0/Current': 'q_a', 'Dc/0/Power': 'q_reported_w',
        'BatteryOperationalLimits/MaxChargeVoltage': 'q_cvl',
        'BatteryOperationalLimits/MaxChargeCurrent': 'q_ccl',
    },
    'battery': {
        'Info/MaxChargeVoltage': 'cvl', 'Info/MaxChargeCurrent': 'ccl',
        'Dc/0/Voltage': 'batt_v', 'Dc/0/Current': 'batt_a', 'Soc': 'soc',
        'RecBms/Sustain/Active': 'hold', 'RecBms/Sustain/Mode': 'hold_mode',
        'RecBms/Sustain/Soc': 'hold_soc', 'RecBms/Sustain/HoldVoltage': 'hold_v',
        'RecBms/Sustain/Servo': 'hold_servo', 'RecBms/Sustain/ChargeLimit': 'hold_ccl',
        'RecBms/Sustain/Status': 'hold_status',
        'RecBms/SolarBoost/Active': 'boost', 'RecBms/SolarBoost/Applied': 'boost_v',
        'RecBms/SolarBoost/WindowOpen': 'boost_window', 'RecBms/SolarBoost/Status': 'boost_status',
        'RecBms/SolarLead': 'lead', 'RecBms/LeadFault': 'lead_fault',
        'RecBms/Voltage/Ready': 'ready',
        'RecBms/Voltage/RequestedQuattro': 'req_q', 'RecBms/Voltage/RequestedSolar': 'req_s',
        'RecBms/Voltage/AcceptedQuattro': 'acc_q', 'RecBms/Voltage/AcceptedSolar': 'acc_s',
        'RecBms/Policy/Telemetry/Transport': 'transport',
        'RecBms/Policy/Telemetry/LimitedBy': 'limited_by',
        'RecBms/Policy/Telemetry/Relay/PendingCommand': 'relay_pending',
        'RecBms/Policy/Telemetry/Relay/LimitedBy': 'relay_limited_by',
        'RecBms/Policy/Telemetry/Relay/Acknowledged': 'relay_acked',
        'RecBms/Policy/Telemetry/Relay/ShoreAvailable': 'relay_shore_available',
        'RecBms/Policy/Telemetry/Relay/ActiveInput': 'relay_active_input',
        'RecBms/Policy/Telemetry/Relay/FaultReason': 'relay_fault',
        'RecBms/Policy/Telemetry/Actuator/Settled': 'settled',
        'RecBms/Policy/Telemetry/Actuator/CommandAcknowledged': 'cmd_acked',
        'RecBms/Policy/Telemetry/Actuator/State': 'actuator_state',
        'RecBms/Policy/Telemetry/Mode': 'mode',
    },
    'system': {
        'Dc/Pv/Power': 'pv_w', 'Dc/Pv/Current': 'pv_a', 'Dc/Battery/Power': 'batt_w',
        'Ac/Consumption/L1/Power': 'ac_w', 'Dc/System/Power': 'dc_w',
        'Control/EffectiveChargeVoltage': 'dvcc_v',
    },
    'switch': {
        'SolarPriority/State': 'sp_state', 'SolarPriority/Status': 'sp_status',
        'SolarPriority/Sustain': 'sp_sustain', 'SolarPriority/OneWay': 'sp_oneway',
        'SolarPriority/Desired': 'sp_desired', 'SolarPriority/LimitedBy': 'sp_limited_by',
    },
    # localsettings (instance 0): the GX's own AC input types -- 0 none,
    # 1 grid, 2 generator, 3 shore power -- which is the owner's statement of
    # which input shore power arrives on, and what both services resolve from.
    'settings': {
        'Settings/SystemSetup/AcInput1': 'ac1_type',
        'Settings/SystemSetup/AcInput2': 'ac2_type',
    },
}
# Per MPPT (one set of fields per instance, suffixed with the instance): the
# yield, the operation mode (1 limited, 2 tracking), the array voltage, the
# output current and the limit DVCC hands it. Needed to tell a curtailed
# charger from a shaded one (2026-09-14: the HOLD's 1 A cap starves one MPPT
# entirely, which the engine's balance read as shade).
MPPT_TOPICS = {'Yield/Power': 'y', 'MppOperationMode': 'm', 'Pv/V': 'voc',
               'Dc/0/Current': 'a', 'Link/ChargeCurrent': 'lim'}
EDGE_FIELDS = ('active_input', 'shore_input', 'ignore_state', 'ignore_cmd', 'shore_available',
               'transport', 'relay_pending', 'hold', 'hold_mode', 'sp_state', 'sp_desired',
               'mode', 'boost')
# Derived per row from the resolved shore input rather than subscribed, so
# every existing reader of these names (and --summary) keeps working whichever
# input shore is on.
DERIVED_FIELDS = ('shore_input', 'ignore_state', 'ignore_cmd', 'shore_available')
SHORE_TYPES = (1, 3)        # grid and shore power; a generator is not shore


def derive(latest, previous):
    """The resolved shore input and the single-input fields taken from it.

    The GX's types decide when exactly one input is grid or shore; failing
    that the accepted input says (ActiveInput 0 = AC in 1, 1 = AC in 2), and
    failing that the trace keeps what it had. Same order the two services
    resolve in (policy_contract.resolve_shore_input).
    """
    t1, t2 = latest.get('ac1_type'), latest.get('ac2_type')
    if t2 in SHORE_TYPES and t1 not in SHORE_TYPES:
        shore = 2
    elif t1 in SHORE_TYPES and t2 not in SHORE_TYPES:
        shore = 1
    elif latest.get('active_input') in (0, 1):
        shore = latest['active_input'] + 1
    else:
        shore = previous if previous in (1, 2) else 1
    return {'shore_input': shore,
            'ignore_state': latest.get('ignore_state%d' % shore),
            'ignore_cmd': latest.get('ignore_cmd%d' % shore),
            'shore_available': latest.get('ac%d_available' % shore)}


def record(args):
    import paho.mqtt.client as mqtt
    instances = {'vebus': args.vebus, 'battery': args.battery, 'system': 0,
                 'switch': args.switch, 'settings': 0}
    lookup = {}
    for service, paths in TOPICS.items():
        for path, field in paths.items():
            lookup['N/%s/%s/%d/%s' % (args.portal, service, instances[service], path)] = field
    for inst in args.mppt:
        for path, field in MPPT_TOPICS.items():
            lookup['N/%s/solarcharger/%d/%s' % (args.portal, inst, path)] = '%s%d' % (field, inst)
    latest = {}
    derived = {}
    edges = []

    def on_message(client, userdata, msg):
        field = lookup.get(msg.topic)
        if field is None:
            return
        try:
            value = json.loads(msg.payload.decode())['value']
        except (ValueError, KeyError, TypeError):
            return
        if field in EDGE_FIELDS and latest.get(field) != value and field in latest:
            edges.append((time.time(), field, latest.get(field), value))
        latest[field] = value

    received = {'at': time.time()}

    def on_connect(client, userdata, flags, rc):
        # (Re)subscribe on every connection: a broker restart or a Wi-Fi
        # drop would otherwise leave a live but deaf client. Precautionary:
        # the 2026-09-14 overnight recorders never actually dropped (they
        # were stopped by hand at 06:43 UTC and misread as stalled).
        for topic in lookup:
            client.subscribe(topic)
        received['at'] = time.time()
        print('%s connected to %s (rc %s), %d topics' % (
            time.strftime('%H:%M:%S', time.gmtime()), hosts[current['i']], rc, len(lookup)), flush=True)
        edges.append((time.time(), 'recorder_host', None, hosts[current['i']]))

    def on_message_wrapped(client, userdata, msg):
        received['at'] = time.time()
        on_message(client, userdata, msg)

    # The Cerbo has two addresses (wifi0 and eth0, the latter only while
    # the Simrad is on) and swapped between them twice on the night of
    # 2026-09-14: each silence is retried on the same host first, then the
    # next host in the list is tried, round robin, so an address change
    # costs the recorder a few minutes rather than the rest of the night.
    hosts = [h.strip() for h in args.host.split(',') if h.strip()]
    current = {'i': 0}
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message_wrapped
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(hosts[0], 1883, 60)
    client.loop_start()
    deadline = time.time() + args.hours * 3600
    last_keepalive = 0.0
    with open(args.out, 'a') as out:
        while time.time() < deadline:
            now = time.time()
            if now - last_keepalive >= 30:
                try:
                    client.publish('R/%s/keepalive' % args.portal, '')
                except Exception as exc:  # the loop thread reconnects; keep recording
                    print('keepalive failed: %s' % exc, flush=True)
                last_keepalive = now
            if now - received['at'] > 120:
                # Nothing for two minutes: the broker publishes on every
                # keepalive, so the connection is dead however it looks.
                print('%s no messages for %.0f s; reconnecting' % (
                    time.strftime('%H:%M:%S', time.gmtime(now)), now - received['at']), flush=True)
                received['at'] = now
                try:
                    client.reconnect()
                except Exception as exc:
                    print('reconnect failed: %s' % exc, flush=True)
                    if len(hosts) > 1:
                        current['i'] = (current['i'] + 1) % len(hosts)
                        print('trying %s' % hosts[current['i']], flush=True)
                        try:
                            client.connect(hosts[current['i']], 1883, 60)
                        except Exception as exc2:
                            print('connect to %s failed: %s' % (hosts[current['i']], exc2), flush=True)
            row = dict(latest)
            row.update(derive(latest, derived.get('shore_input')))
            for field in DERIVED_FIELDS:
                if field in EDGE_FIELDS and field in derived and derived[field] != row[field]:
                    edges.append((now, field, derived[field], row[field]))
                derived[field] = row[field]
            row['t'] = round(now, 1)
            if now - received['at'] > 10:
                # Nothing heard for a while: the values are the last ones
                # seen, not the boat's present state. Marked so summaries
                # skip them (2026-09-14 23:01Z: the Cerbo changed address
                # and 5 min of stale copies were written before anyone knew).
                row['stale_s'] = round(now - received['at'])
            row['iso'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
            while edges:
                stamp, field, old, new = edges.pop(0)
                out.write(json.dumps({'t': round(stamp, 1), 'edge': field, 'from': old, 'to': new}) + '\n')
                print('%s %s: %s -> %s' % (time.strftime('%H:%M:%S', time.gmtime(stamp)), field, old, new), flush=True)
            out.write(json.dumps(row, sort_keys=True) + '\n')
            out.flush()
            time.sleep(max(0.0, 1.0 - (time.time() - now)))
    client.loop_stop()


def summary(path):
    rows, edges = [], []
    with open(path) as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('stale_s'):
                continue
            (edges if 'edge' in item else rows).append(item)
    if not rows:
        print('no rows')
        return
    print('%d rows, %s .. %s' % (len(rows), rows[0]['iso'], rows[-1]['iso']))
    for edge in edges:
        if edge['edge'] != 'active_input':
            continue
        stamp = edge['t']
        before = [r for r in rows if r['t'] <= stamp][-3:]
        after = [r for r in rows if stamp < r['t'] <= stamp + 900]
        closing = edge['to'] in (0, 1)
        print('\n%s ActiveInput %s -> %s (%s)' % (
            time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(stamp)), edge['from'], edge['to'],
            'RETURN to AC' if closing else 'DEPARTURE'))
        for r in before[-1:]:
            print('  at the edge: pack %sV %sA  cvl %s ccl %s  q_cvl %s q_ccl %s  hold %s/%s %sV  ready %s  transport %s  limited_by %r' % (
                r.get('batt_v'), r.get('batt_a'), r.get('cvl'), r.get('ccl'), r.get('q_cvl'), r.get('q_ccl'),
                r.get('hold'), r.get('hold_mode'), r.get('hold_v'), r.get('ready'), r.get('transport'), r.get('limited_by')))
        if closing and after:
            peak = max(after, key=lambda r: (r.get('q_v') or 0) * (r.get('q_a') or 0))
            tail_wh = sum(max(0.0, (r.get('q_v') or 0) * (r.get('q_a') or 0)) for r in after) / 3600.0
            batt_wh = sum(max(0.0, r.get('batt_w') or 0) for r in after) / 3600.0
            settled = next((r for r in after if r.get('q_a') is not None and abs(r['q_a']) <= 1.0 and r['t'] - stamp > 10), None)
            print('  next 15 min: Quattro peak %.0f W at +%.0f s, Quattro charge %.1f Wh, battery charge %.1f Wh, |Quattro I| <= 1 A first at %s' % (
                (peak.get('q_v') or 0) * (peak.get('q_a') or 0), peak['t'] - stamp, tail_wh, batt_wh,
                ('+%.0f s' % (settled['t'] - stamp)) if settled else 'never'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='192.168.50.107,192.168.50.170',
                    help='comma-separated addresses to try in turn (wifi0 first, eth0 second)')
    ap.add_argument('--portal', default='102c6b8611dd')
    ap.add_argument('--vebus', type=int, default=276)
    ap.add_argument('--battery', type=int, default=200)
    ap.add_argument('--switch', type=int, default=221)
    ap.add_argument('--mppt', default='278,279', help='solarcharger instances to record per charger')
    ap.add_argument('--out', default='solar-trace.jsonl')
    ap.add_argument('--hours', type=float, default=12.0)
    ap.add_argument('--summary', metavar='TRACE', help='summarise a recorded trace and exit')
    args = ap.parse_args()
    args.mppt = [int(x) for x in args.mppt.split(',') if x.strip()]
    if args.summary:
        summary(args.summary)
        return
    record(args)


if __name__ == '__main__':
    main()
