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
            if type(limits.get('sustain')) is not int or limits['sustain'] not in (0, 1, 2, 3):
                raise ValueError('sustain must be release (0), floor (1), ceiling (2) or hold (3)')
            if request['mode'] in ('OFF', 'COMPLETE_FULL') and limits['sustain'] != 0:
                raise ValueError('disabled/full mode must release sustain')
            if limits.get('purpose', '') not in ('', 'solar', 'probe', 'buffer', 'descent', 'failed_probe'):
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


# GX "AC input type" settings (/Settings/SystemSetup/AcInput1|2): 0 = not
# available, 1 = grid, 2 = generator, 3 = shore power. Grid and shore are
# both a supply the boat may leave for the sun; a generator is not.
SHORE_INPUT_TYPES = (1, 3)


def resolve_shore_input(configured, types, active_input, available, last):
    """Which Quattro AC input carries shore power: 1 or 2, with the reason.

    A fixed `configured` (1 or 2) wins. Otherwise ('auto') the GX's own AC
    input types decide when exactly one input is grid or shore -- the owner
    sets those when rewiring, and they are the semantic answer whatever the
    relay is doing. Failing that, the input already resolved is KEPT: a
    box that has settled on an input never moves off it on ambiguous
    evidence (an island reads ActiveInput 240 and no availability at all).
    Only a fresh start with nothing to go on reads the live facts -- the
    accepted input (/Ac/ActiveIn/ActiveInput 0 = AC in 1, 1 = AC in 2), then
    the single available input -- and the last resort is input 1. Pure, so
    both services can share it and it can be tested off the boat.
    """
    if configured in (1, 2):
        return int(configured), 'configured'
    t1, t2 = (types or (None, None))[:2]
    typed = [n for n, t in ((1, t1), (2, t2)) if t in SHORE_INPUT_TYPES]
    if len(typed) == 1:
        return typed[0], 'gx input type'
    if last in (1, 2):
        return int(last), 'kept'
    if active_input in (0, 1):
        return int(active_input) + 1, 'accepted input'
    a1, a2 = (available or (None, None))[:2]
    present = [n for n, a in ((1, a1), (2, a2)) if a == 1]
    if len(present) == 1:
        return present[0], 'only input available'
    return 1, 'default'


class TransferSupervisor:
    """All relay purposes share dwell, feedback accounting, fault and backoff.

    step returns a command only after preparation or for protective return.
    Call command_result with the actual SetValue reply; feedback separately
    identifies physical edges. Reassertions never add edges or departures.

    Four observations stay separate (SP56, master review D14, boat readings
    2026-09-14 on vebus 276): the ignore command this supervisor writes, the
    Quattro's acknowledgment of it (/Ac/State/IgnoreAcInN), whether that
    supply is available at all (/Ac/State/AcInNAvailable), and which input is
    actually accepted (/Ac/ActiveIn/ActiveInput: 0 = AC1, 1 = AC2, 240 =
    none). An absent shore supply is not a refused relay write, and "not AC1"
    alone is not proof of inverter-only operation.
    """
    def __init__(self, state=None, connected_dwell_s=300.0, timeout_s=30.0,
                 hourly_departures=3, daily_departures=12, backoff_s=900.0,
                 prepare_s=30.0):
        self.durable = state if state is not None else {}
        # New keys use setdefault so a ledger written by an older REC still
        # loads with its spent allowances intact.
        for key, default in (('departures', []), ('edges', []), ('fault_until', 0.0),
                             ('backoff_until', 0.0), ('failures', 0), ('external_edges', 0),
                             ('logical_s', 0.0), ('last_wall_s', None), ('last_fault', None),
                             ('probe_failures', []), ('last_departure_s', None),
                             ('last_failed_departure', None), ('shore_input', None)):
            self.durable.setdefault(key, default)
        self.connected_dwell_s = connected_dwell_s
        self.timeout_s = timeout_s
        self.hourly_departures = hourly_departures
        self.daily_departures = daily_departures
        self.backoff_s = backoff_s
        self.prepare_s = prepare_s
        self.state = 'PREPARE_CONNECT'
        self.feedback = None
        self.connected_since = None
        self.pending = None
        self.pending_since = None
        self.last_command = None
        self.last_assert = None
        self.last_acknowledged = None
        self.last_acknowledged_at = None
        self.last_edge_at = None
        self.available = None
        self.ignore_state = None
        self.active_input = None
        self.other_input = False
        self.acknowledged = False
        self.acknowledged_at = None
        self.shore_restored = False
        self.prepare_since = None
        self.prepared = None
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
        for key in ('departures', 'edges', 'probe_failures'):
            self.durable[key] = [t for t in self.durable[key] if current - t < 86400]

    def observe(self, connected, now, wall, available=None, ignore_state=None,
                active_input=None):
        """Record this tick's physical facts; keyword inputs stay optional.

        ``available`` is the configured input's AcInNAvailable (True/False, or
        None when the path is absent or stale -- missing availability stays
        unknown). ``ignore_state`` is the Quattro's acknowledgment of the
        ignore command, ``active_input`` the accepted input (0/1, None when
        none is accepted).
        """
        stamp = self._advance(now, wall)
        # A False -> True availability edge earns one urgent reassertion: the
        # connect command was written into a supply that was not there.
        if available is True and self.available is False:
            self.shore_restored = True
        self.available = available
        self.ignore_state = ignore_state
        self.active_input = active_input
        self.other_input = bool(active_input is not None and connected is not None
                                and not connected)
        # Command acknowledgment is the Quattro echoing the ignore state we
        # asked for; it is not the AC input being accepted (SP56/SP63).
        expected = self.pending if self.pending is not None else self.last_command
        self.acknowledged = bool(ignore_state is not None and expected is not None
                                 and ignore_state == expected)
        if not self.acknowledged:
            self.acknowledged_at = None
        elif self.acknowledged_at is None:
            self.acknowledged_at = now
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
            self.last_edge_at = now
            if self.pending is None or (self.pending == 0) != connected:
                self.durable['external_edges'] += 1
        if connected and self.feedback is not True:
            self.connected_since = now
            self.prepare_since = None
        elif not connected:
            self.connected_since = None
        self.feedback = connected
        if self.pending is not None and (self.pending == 0) == connected:
            self.last_acknowledged = self.pending
            self.last_acknowledged_at = now
            if self.pending == 1:
                # The physical departure, not the reserved attempt: a refused
                # or timed-out transfer never reaches this point (D16).
                self.durable['last_departure_s'] = stamp
            self.pending = None
            self.pending_since = None
        if connected and self.state == 'PREPARE_CONNECT':
            self.state = 'CONNECTED'
        elif not connected and self.state == 'PREPARE_ISLAND' and self.last_command == 1:
            self.state = 'ISLANDED'
        # Do not infer inverter-only operation merely because AC1 is inactive:
        # with another input accepted the bank is on a charger, not islanded.
        if self.other_input and self.state == 'ISLANDED':
            self.state = 'PREPARE_CONNECT'

    def departure_reason(self, now, wall):
        stamp = self._advance(now, wall)
        if stamp < self.durable['fault_until']:
            return 'transfer fault lockout'
        if stamp < self.durable['backoff_until']:
            return 'failed probe backoff'
        if self.other_input:
            return 'another AC input accepted'
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
        """One evaluated failed probe; the escalation decays (issue #8, D16).

        ``failures`` stays the lifetime counter reported by telemetry, but the
        delay comes from the failures inside the last 24 h of logical uptime,
        so a bad afternoon cannot hold the 4 h backoff for weeks.
        """
        stamp = self.durable['logical_s'] if now is None else self._advance(now, wall)
        self.durable['last_wall_s'] = wall
        self.durable['failures'] += 1
        self.durable['probe_failures'].append(stamp)
        recent = sum(1 for t in self.durable['probe_failures'] if stamp - t < 86400)
        delay = min(14400, self.backoff_s * 2 ** min(4, max(1, recent) - 1))
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

    def _timeout_reason(self):
        """SP63: absent supply, refused command and delayed acceptance differ."""
        if self.ignore_state is None:
            return 'relay feedback timeout'
        if self.ignore_state == self.pending:
            return 'relay command acknowledged, AC input not accepted'
        return 'relay command not acknowledged'

    def _issue(self, value, now, wall, urgent=False):
        """Every command actually issued from step clears the preparation timer."""
        command = self._command(value, now, wall, urgent=urgent)
        if command is not None:
            self.prepare_since = None
            self.shore_restored = False
            if value == 1:
                self.prepared = None
        return command

    def step(self, intent, now, wall, ready, permitted, protective=False):
        self._advance(now, wall)
        # A queued departure may not yet be visible in feedback. Supersede it
        # immediately when its preparation, permission or intent is withdrawn.
        if self.pending == 1 and (intent in ('connected', 'protect') or
                                  protective or self.feedback is None or not ready or not permitted):
            self.state = 'PREPARE_CONNECT'
            self.limited_by = 'pending departure cancelled'
            return self._issue(0, now, wall, urgent=True)
        if self.pending_since is not None and now - self.pending_since >= self.timeout_s:
            if self.pending == 0 and self.available is False:
                # E13/D14: the connect command was written -- possibly even
                # acknowledged -- and feedback stays disconnected because no
                # shore supply is there to accept. That is an observation, not
                # a refused relay write: no fault and no 3600 s lockout. The
                # command stands and is reasserted on the ordinary cadence.
                self.pending = None
                self.pending_since = None
                self.state = 'PREPARE_CONNECT'
                self.limited_by = 'shore unavailable'
            else:
                self.fault(now, wall, self._timeout_reason())
                protective = True
        if protective or intent == 'protect' or self.feedback is None:
            self.state = 'PREPARE_CONNECT' if self.feedback is not True else 'CONNECTED'
            return self._issue(0, now, wall, urgent=True)
        if intent == 'connected':
            if self.feedback is True:
                self.state = 'CONNECTED'
                self.prepare_since = None
                if self.limited_by in ('preparing shore protection', 'shore unavailable'):
                    self.limited_by = ''
                return None
            self.state = 'PREPARE_CONNECT'
            if self.shore_restored and self.pending == 0:
                # The outstanding write went into a supply that was not there;
                # its feedback clock belongs to the supply that just appeared.
                self.pending = None
                self.pending_since = None
            if not ready and self.available is False:
                # Nothing to prepare for: the command goes out so that the
                # supply is taken the moment it is back, and the timeout
                # below records 'shore unavailable' rather than a fault. The
                # restored supply re-enters this branch through shore_restored
                # and waits for its preparation like any other return.
                self.prepared = None
                return self._issue(0, now, wall)
            if not ready:
                # A1/D02: an ordinary islanded return waits for the exact
                # requested pair and a current readback inside the envelope,
                # so the below-pack Quattro command is in force BEFORE the
                # relay closes (E03 closed at pack 56.41 V against CVL
                # 59.34 V and CCL 200 A, reductions arriving +3 s/+5 s later).
                if self.prepare_since is None:
                    self.prepare_since = now
                waited = now - self.prepare_since
                if waited < self.prepare_s:
                    self.limited_by = 'preparing shore protection'
                    return None
                # A3: failed preparation may not strand an urgent return. Not
                # a fault and no lockout -- dbus-recbms already raises the
                # unverified pair as a regulation fault after lead_verify_s,
                # and REC's own guards still decide the current (CCL 0 outside
                # the envelope, PV + charge_limit_a under a hold).
                self.prepared = False
                self.limited_by = 'unprepared return after %.0fs' % waited
                return self._issue(0, now, wall, urgent=True)
            self.prepared = True
            return self._issue(0, now, wall, urgent=self.shore_restored)
        if intent != 'connected':
            # No return is being prepared: a wait started by a transient
            # 'connected' must not keep counting under an island intent
            # (boat, 2026-09-14 16:06 UTC: a timer left running through a
            # permission flicker closed the relay unprepared 33 s later).
            self.prepare_since = None
        if (self.feedback is False and self.last_command == 1 and not self.other_input and
                self.state == 'PREPARE_CONNECT' and ready and permitted):
            # We departed, a return was begun and withdrawn, and the island
            # is judged good again: it is an island, not a pending return.
            self.state = 'ISLANDED'
            self.prepared = None
            self.limited_by = ''
        if self.state == 'ISLANDED' and self.feedback is False:
            if ready and permitted:
                self.limited_by = ''
                return None
            self.state = 'PREPARE_CONNECT'
            return self._issue(0, now, wall)
        reason = self.departure_reason(now, wall)
        if reason or not permitted or not ready:
            self.limited_by = reason or ('policy preparation pending' if not ready else 'departure not permitted')
            return None
        self.state = 'PREPARE_ISLAND'
        self.limited_by = ''
        if intent == 'island':
            return self._issue(1, now, wall)
        return None

    def snapshot(self, now, wall):
        """The return timeline, each stage named for what it actually proves.

        ``command_at`` is the last assertion of the ignore command,
        ``acknowledged_at`` the Quattro's acknowledgment of that command,
        ``accepted_at`` the physical acceptance of the AC input, and
        ``transition_age_s`` the age of the last physical relay edge. All are
        monotonic seconds (SP62: request, acknowledgment and physical
        transfer are timestamped separately).
        """
        stamp = self._advance(now, wall)
        return {'state': self.state, 'connected': self.feedback,
                'pending': self.pending,
                'pending_age_s': None if self.pending_since is None else max(0.0, now - self.pending_since),
                'available': self.available, 'ignore_state': self.ignore_state,
                'active_input': self.active_input, 'acknowledged': self.acknowledged,
                'prepared': self.prepared, 'prepare_started_at': self.prepare_since,
                'prepare_age_s': None if self.prepare_since is None else max(0.0, now - self.prepare_since),
                'command_at': self.last_assert, 'acknowledged_at': self.acknowledged_at,
                'accepted_at': self.last_acknowledged_at,
                'transition_age_s': None if self.last_edge_at is None else max(0.0, now - self.last_edge_at),
                'last_fault': self.durable['last_fault'], 'actual_edges_24h': len(self.durable['edges']),
                'departures_24h': len(self.durable['departures']),
                'external_edges': self.durable['external_edges'],
                'failures': self.durable['failures'],
                'next_departure_s': max(0.0, self.connected_dwell_s -
                    (0 if self.connected_since is None else now - self.connected_since),
                    self.durable['fault_until'] - stamp, self.durable['backoff_until'] - stamp),
                'limited_by': self.limited_by}
