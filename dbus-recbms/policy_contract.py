"""Versioned REC policy requests and the single shore-transfer supervisor.

Durable policy and relay budgets survive owner restarts; authority to perform
an island transfer expires against monotonic time and is never restored.
"""
import json
import math
import uuid

VERSION = 2
MODES = ('OFF', 'CHARGE', 'DISCHARGE', 'HOLD', 'COMPLETE_FULL')
INTENTS = ('connected', 'prepare_island', 'island', 'protect')


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError('%s must be numeric' % name)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('%s out of range' % name)
    return float(value)


class PolicyContract:
    def __init__(self, state=None, generation=None):
        self.state = state if state is not None else {}
        self.generation = generation or uuid.uuid4().hex
        self.accepted_id = 0
        self.request = None
        self.expires = 0.0
        self.rejection = ''
        self.state.setdefault('owned', False)

    @property
    def owned(self):
        return self.state['owned']

    def accept(self, payload, now, target_soc, consumer_ready=False):
        try:
            request = json.loads(payload) if isinstance(payload, str) else dict(payload)
            required = {'version', 'generation', 'request_id', 'mode', 'target_soc',
                        'transfer_intent', 'requested_limits', 'lease_s'}
            if set(request) != required:
                raise ValueError('unexpected or missing request fields')
            if request['version'] != VERSION or request['generation'] != self.generation:
                raise ValueError('publisher generation/version mismatch')
            rid = request['request_id']
            if type(rid) is not int or rid <= self.accepted_id:
                raise ValueError('request is stale or reordered')
            if request['mode'] not in MODES or request['transfer_intent'] not in INTENTS:
                raise ValueError('unknown policy or transfer intent')
            target = _number(request['target_soc'], 0, 100, 'target_soc')
            if abs(target - target_soc) > 0.001:
                raise ValueError('target does not match REC slider')
            lease = _number(request['lease_s'], 1, 120, 'lease_s')
            limits = request['requested_limits']
            if not isinstance(limits, dict) or set(limits) - {'boost_v', 'purpose', 'sustain'}:
                raise ValueError('unknown requested limits')
            _number(limits.get('boost_v', 0), 0, 0.30, 'boost_v')
            if type(limits.get('sustain')) is not int or limits['sustain'] not in (0, 1, 2):
                raise ValueError('sustain must be release (0), floor (1), or ceiling (2)')
            if request['mode'] in ('OFF', 'COMPLETE_FULL') and limits['sustain'] != 0:
                raise ValueError('disabled/full mode must release sustain')
            if limits.get('purpose', '') not in ('', 'solar', 'probe', 'buffer', 'descent'):
                raise ValueError('unknown transfer purpose')
            if not self.owned and not consumer_ready:
                raise ValueError('new consumer ownership handover not verified')
            self.state['owned'] = True
            self.state['policy'] = {'mode': request['mode'], 'target_soc': target}
            self.request = request
            self.accepted_id = rid
            self.expires = now + lease
            self.rejection = ''
            return True
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.rejection = str(exc)
            return False

    def active(self, now, target_soc=None):
        return (self.request is not None and 0 < self.expires - now <= 120 and
                (target_soc is None or abs(self.request['target_soc'] - target_soc) <= 0.001))

    def status(self, now):
        return {'version': VERSION, 'generation': self.generation,
                'accepted_id': self.accepted_id, 'owned': self.owned,
                'lease_remaining_s': max(0.0, self.expires - now),
                'lease_valid': self.active(now), 'rejection': self.rejection}


class TransferSupervisor:
    """All relay purposes share dwell, feedback accounting, fault and backoff.

    step returns a command only after preparation or for protective return.
    Call command_result with the actual SetValue reply; feedback separately
    identifies physical edges. Reassertions never add edges or departures.
    """
    def __init__(self, state=None, connected_dwell_s=300.0, timeout_s=30.0,
                 hourly_departures=3, daily_departures=12, backoff_s=900.0):
        self.durable = state if state is not None else {}
        for key, default in (('departures', []), ('edges', []), ('fault_until', 0.0),
                             ('backoff_until', 0.0), ('failures', 0), ('external_edges', 0),
                             ('logical_s', 0.0), ('last_wall_s', None), ('last_fault', None)):
            self.durable.setdefault(key, default)
        self.connected_dwell_s = connected_dwell_s
        self.timeout_s = timeout_s
        self.hourly_departures = hourly_departures
        self.daily_departures = daily_departures
        self.backoff_s = backoff_s
        self.state = 'PREPARE_CONNECT'
        self.feedback = None
        self.connected_since = None
        self.pending = None
        self.pending_since = None
        self.last_command = None
        self.last_assert = None
        self.last_acknowledged = None
        self.last_acknowledged_at = None
        self.limited_by = 'startup: confirming shore'
        self._clock_mono = None

    def _advance(self, now, wall):
        """Age persistent allowances only by observed monotonic uptime.

        A process restart cannot prove its downtime was uneventful; it retains
        spent allowance until enough new uptime elapses. Wall time is telemetry
        only, so RTC/NTP corrections cannot grant new relay or retry credit.
        """
        if not math.isfinite(now):
            raise ValueError('invalid monotonic time')
        if self._clock_mono is not None:
            self.durable['logical_s'] += max(0.0, now - self._clock_mono)
        self._clock_mono = now if self._clock_mono is None else max(now, self._clock_mono)
        self.durable['last_wall_s'] = wall
        self._prune()
        return self.durable['logical_s']

    def _prune(self):
        current = self.durable['logical_s']
        for key in ('departures', 'edges'):
            self.durable[key] = [t for t in self.durable[key] if current - t < 86400]

    def observe(self, connected, now, wall):
        stamp = self._advance(now, wall)
        if connected is None:
            self.feedback = None
            self.connected_since = None
            self.limited_by = 'shore feedback unavailable'
            return
        connected = bool(connected)
        if self.limited_by == 'shore feedback unavailable':
            self.limited_by = ''
        if self.feedback is not None and connected != self.feedback:
            self.durable['edges'].append(stamp)
            if self.pending is None or (self.pending == 0) != connected:
                self.durable['external_edges'] += 1
        if connected and self.feedback is not True:
            self.connected_since = now
        elif not connected:
            self.connected_since = None
        self.feedback = connected
        if self.pending is not None and (self.pending == 0) == connected:
            self.last_acknowledged = self.pending
            self.last_acknowledged_at = now
            self.pending = None
            self.pending_since = None
        if connected and self.state == 'PREPARE_CONNECT':
            self.state = 'CONNECTED'
        elif not connected and self.state == 'PREPARE_ISLAND' and self.last_command == 1:
            self.state = 'ISLANDED'

    def departure_reason(self, now, wall):
        stamp = self._advance(now, wall)
        if stamp < self.durable['fault_until']:
            return 'transfer fault lockout'
        if stamp < self.durable['backoff_until']:
            return 'failed probe backoff'
        if self.connected_since is None or now - self.connected_since < self.connected_dwell_s:
            return 'minimum connected dwell'
        departures = self.durable['departures']
        if sum(stamp - t < 3600 for t in departures) >= self.hourly_departures:
            return 'hourly departure budget'
        if len(departures) >= self.daily_departures:
            return 'daily departure budget'
        return ''

    def fault(self, now, wall, reason):
        stamp = self._advance(now, wall)
        self.durable['last_fault'] = {
            'reason': reason, 'command': self.pending, 'wall_s': wall,
            'logical_s': stamp, 'feedback_connected': self.feedback,
            'pending_age_s': None if self.pending_since is None else max(0.0, now - self.pending_since)}
        self.durable['fault_until'] = max(self.durable['fault_until'], stamp + 3600)
        self.limited_by = reason
        self.state = 'PREPARE_CONNECT'
        self.pending = None
        self.pending_since = None

    def failed_probe(self, wall, now=None):
        stamp = self.durable['logical_s'] if now is None else self._advance(now, wall)
        self.durable['last_wall_s'] = wall
        self.durable['failures'] += 1
        delay = min(14400, self.backoff_s * 2 ** min(4, self.durable['failures'] - 1))
        self.durable['backoff_until'] = max(self.durable['backoff_until'], stamp + delay)

    def command_result(self, value, code, now, wall):
        if code != 0:
            self.fault(now, wall, 'relay SetValue refused (%s)' % code)
            self.last_command = None

    def _command(self, value, now, wall, urgent=False):
        # Confirmed shore needs no repeated write. Cancellation, refusal and
        # timeout still get an immediate assertion despite cached feedback.
        if (value == 0 and self.feedback is True and self.pending is None and
                self.last_command == 0 and self.last_acknowledged == 0 and
                self.last_acknowledged_at is not None and self.last_assert is not None and
                self.last_acknowledged_at >= self.last_assert):
            return None
        if self.pending == value or (not urgent and self.last_command == value and
                                    self.last_assert is not None and now - self.last_assert < 30):
            return None
        self.pending = value
        self.pending_since = now
        self.last_command = value
        self.last_assert = now
        if value == 1:
            # Reserve before a reply or feedback can arrive; refusal is not free retry credit.
            self.durable['departures'].append(self.durable['logical_s'])
        return value

    def step(self, intent, now, wall, ready, permitted, protective=False):
        self._advance(now, wall)
        # A queued departure may not yet be visible in feedback. Supersede it
        # immediately when its preparation, permission or intent is withdrawn.
        if self.pending == 1 and (intent in ('connected', 'protect') or
                                  protective or self.feedback is None or not ready or not permitted):
            self.state = 'PREPARE_CONNECT'
            self.limited_by = 'pending departure cancelled'
            return self._command(0, now, wall, urgent=True)
        if self.pending_since is not None and now - self.pending_since >= self.timeout_s:
            self.fault(now, wall, 'relay feedback timeout')
            protective = True
        if protective or intent == 'protect' or self.feedback is None:
            self.state = 'PREPARE_CONNECT' if self.feedback is not True else 'CONNECTED'
            return self._command(0, now, wall, urgent=True)
        if intent == 'connected':
            if self.feedback is True:
                self.state = 'CONNECTED'
                return None
            self.state = 'PREPARE_CONNECT'
            if not ready:
                self.limited_by = 'waiting for shore protection'
                return None
            return self._command(0, now, wall)
        if self.state == 'ISLANDED' and self.feedback is False:
            if ready and permitted:
                return None
            self.state = 'PREPARE_CONNECT'
            return self._command(0, now, wall)
        reason = self.departure_reason(now, wall)
        if reason or not permitted or not ready:
            self.limited_by = reason or ('policy preparation pending' if not ready else 'departure not permitted')
            return None
        self.state = 'PREPARE_ISLAND'
        self.limited_by = ''
        if intent == 'island':
            return self._command(1, now, wall)
        return None

    def snapshot(self, now, wall):
        stamp = self._advance(now, wall)
        return {'state': self.state, 'connected': self.feedback,
                'pending': self.pending,
                'pending_age_s': None if self.pending_since is None else max(0.0, now - self.pending_since),
                'last_fault': self.durable['last_fault'], 'actual_edges_24h': len(self.durable['edges']),
                'departures_24h': len(self.durable['departures']),
                'external_edges': self.durable['external_edges'],
                'next_departure_s': max(0.0, self.connected_dwell_s -
                    (0 if self.connected_since is None else now - self.connected_since),
                    self.durable['fault_until'] - stamp, self.durable['backoff_until'] - stamp),
                'limited_by': self.limited_by}
