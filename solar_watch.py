#!/usr/bin/env python3
"""Record a Solar Priority transfer trace from the Cerbo over MQTT (read-only).

Subscribes to the handful of paths that describe a shore departure or return
-- the Quattro's active input, the ignore command and its acknowledged state,
shore availability, the commanded and accepted voltage pair, the CCL, the
sustain hold, the policy transport state and the signed Quattro V x I -- and
writes one JSON line per second to a file, plus a line on every relay edge.
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
        'Ac/State/IgnoreAcIn1': 'ignore_state',
        'Ac/Control/IgnoreAcIn1': 'ignore_cmd',
        'Ac/State/AcIn1Available': 'shore_available',
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
}
EDGE_FIELDS = ('active_input', 'ignore_state', 'ignore_cmd', 'shore_available', 'transport',
               'relay_pending', 'hold', 'hold_mode', 'sp_state', 'sp_desired', 'mode')


def record(args):
    import paho.mqtt.client as mqtt
    instances = {'vebus': args.vebus, 'battery': args.battery, 'system': 0, 'switch': args.switch}
    lookup = {}
    for service, paths in TOPICS.items():
        for path, field in paths.items():
            lookup['N/%s/%s/%d/%s' % (args.portal, service, instances[service], path)] = field
    latest = {}
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
        # drop otherwise leaves a live but deaf client (seen 2026-09-14 06:43
        # UTC, both recorders silent from the same second).
        for topic in lookup:
            client.subscribe(topic)
        received['at'] = time.time()
        print('%s connected (rc %s), %d topics' % (time.strftime('%H:%M:%S', time.gmtime()), rc, len(lookup)), flush=True)

    def on_message_wrapped(client, userdata, msg):
        received['at'] = time.time()
        on_message(client, userdata, msg)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message_wrapped
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(args.host, 1883, 60)
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
            row = dict(latest)
            row['t'] = round(now, 1)
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
    ap.add_argument('--host', default='192.168.50.107')
    ap.add_argument('--portal', default='102c6b8611dd')
    ap.add_argument('--vebus', type=int, default=276)
    ap.add_argument('--battery', type=int, default=200)
    ap.add_argument('--switch', type=int, default=221)
    ap.add_argument('--out', default='solar-trace.jsonl')
    ap.add_argument('--hours', type=float, default=12.0)
    ap.add_argument('--summary', metavar='TRACE', help='summarise a recorded trace and exit')
    args = ap.parse_args()
    if args.summary:
        summary(args.summary)
        return
    record(args)


if __name__ == '__main__':
    main()
