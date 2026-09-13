"""REC-owned battery accounting and maintenance evidence, independent of D-Bus.

Positive battery current/power means charging. SOC is display/reference evidence,
never an energy input. All durations and rolling-budget ageing use monotonic
time; a restart cannot age away a reservation or bridge an unmeasured interval.
The default wear/evidence parameters are simulation proposals, not pack ratings.
"""

import copy
import datetime
import json
import math
import os
import tempfile


WINDOW_S = 86400.0
SCHEMA_VERSION = 1
OVERHEAD_CATEGORIES = frozenset(("buffer", "probe", "maintenance", "reverse",
                                 "shore_refill", "control_error"))
POLICY_MODES = frozenset(("OFF", "CHARGE", "DISCHARGE", "HOLD",
                          "COMPLETE_FULL", "HOLD_FULL", "MAINTENANCE"))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value, name, allow_zero=False):
    if not _number(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError("%s must be finite and %s" % (name, "nonnegative" if allow_zero else "positive"))
    return float(value)


def _totals():
    return {"charge_ah": 0.0, "discharge_ah": 0.0, "charge_wh": 0.0,
            "discharge_wh": 0.0, "efc": 0.0}


def _integrate_signed(start, end, hours):
    """Integrate a linear sample segment, splitting an actual zero crossing.

    Averaging before separating directions cancels two opposite legs. Counting
    two endpoint rectangles instead invents throughput at a zero crossing.
    """
    if start >= 0 and end >= 0:
        return (start + end) * hours / 2.0, 0.0
    if start <= 0 and end <= 0:
        return 0.0, -(start + end) * hours / 2.0
    fraction = abs(start) / (abs(start) + abs(end))
    first = abs(start) * fraction * hours / 2.0
    second = abs(end) * (1.0 - fraction) * hours / 2.0
    return (first, second) if start > 0 else (second, first)


class EnergyLedger:
    """Persistent measured throughput, directional references and admission cost.

    A reservation holds the *whole* expected cycle before its first leg. Actual
    throughput replaces the reserved amount as it is measured. An interrupted
    reservation consumes its remainder conservatively; only verified completion
    releases unused allowance. The rolling limit is charged immediately on a
    discharge, because its expected return leg was already reserved.

    With ``path=None`` the caller explicitly starts a known, empty offline ledger.
    Missing/corrupt on-disk history and every process restart withhold new
    discretionary allowance for 24 hours of observed monotonic uptime. Historical
    totals remain lower bounds after a gap, which is exposed in telemetry.
    """

    def __init__(self, capacity_ah=1440.0, capacity_version="rated-1440", path=None,
                 max_gap_s=10.0, max_overhead_efc=0.02, reverse_event_wh=20.0,
                 reverse_day_wh=100.0, checkpoint_s=60.0, nominal_voltage_v=58.0,
                 calendar_timezone=datetime.timezone.utc):
        self.capacity_ah = _positive(capacity_ah, "capacity_ah")
        if not isinstance(capacity_version, str) or not capacity_version:
            raise ValueError("capacity_version must be a nonempty string")
        self.capacity_version = capacity_version
        self.nominal_voltage_v = _positive(nominal_voltage_v, "nominal_voltage_v")
        if isinstance(calendar_timezone, str):
            if calendar_timezone == 'UTC':
                calendar_timezone = datetime.timezone.utc
            else:
                try:
                    from zoneinfo import ZoneInfo
                    calendar_timezone = ZoneInfo(calendar_timezone)
                except (ImportError, KeyError, ValueError) as exc:
                    raise ValueError('calendar timezone requires a valid IANA zone and zoneinfo database') from exc
        if not isinstance(calendar_timezone, datetime.tzinfo):
            raise ValueError("calendar_timezone must be a datetime.tzinfo")
        self.calendar_timezone = calendar_timezone
        self.path = os.fspath(path) if path is not None else None
        self.max_gap_s = _positive(max_gap_s, "max_gap_s")
        self.max_overhead_efc = _positive(max_overhead_efc, "max_overhead_efc")
        self.reverse_event_limit_wh = _positive(reverse_event_wh, "reverse_event_wh")
        self.reverse_day_limit_wh = _positive(reverse_day_wh, "reverse_day_wh")
        self.checkpoint_s = _positive(checkpoint_s, "checkpoint_s")
        self._state = {"schema_version": SCHEMA_VERSION, "total": _totals(),
                       "overhead": _totals(), "capacities": {}, "buckets": [],
                       "reservations": {}, "controller_state": {}, "calendar_days": {},
                       "logical_s": 0.0, "wall_s": None, "uncertain_until": 0.0,
                       "uncertainty_reason": "", "complete_history": True,
                       "gap_count": 0, "net_ah": 0.0, "net_wh": 0.0,
                       "references": {"mode": "OFF", "target_soc": None,
                                      "high_ah": 0.0, "low_ah": 0.0,
                                      "high_wh": 0.0, "low_wh": 0.0,
                                      "high_soc": None, "low_soc": None},
                       "reverse": {"event_wh": 0.0, "total_wh": 0.0},
                       "recovery": {"charge_ah": 0.0, "discharge_ah": 0.0},
                       "buffer": {"credit_ah": 0.0, "credit_wh": 0.0}}
        self._last = None
        self._clock_mono = None
        self._checkpoint_at = 0.0
        self.persistence_error = ""
        if self.path is not None:
            self._restore()
        capacities = self._state["capacities"]
        old = capacities.get(capacity_version)
        if old is not None and old["capacity_ah"] != self.capacity_ah:
            raise ValueError("changed capacity requires a new capacity_version")
        if old is not None and old["nominal_voltage_v"] != self.nominal_voltage_v:
            raise ValueError("changed energy reference requires a new capacity_version")
        capacities.setdefault(capacity_version, {"capacity_ah": self.capacity_ah,
                                                 "nominal_voltage_v": self.nominal_voltage_v,
                                                 "total": _totals(), "overhead": _totals()})

    @property
    def controller_state(self):
        """JSON state owned by the REC controller, committed with this ledger."""
        return self._state["controller_state"]

    def _restore(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                candidate = json.load(handle)
            self._validate(candidate)
            self._state = candidate
            self.mark_gap("process_restart")
            # Actuation leases do not survive the process. Reserve their unknown
            # remainder in rolling history, where it can age out conservatively.
            for reservation_id in list(self._state["reservations"]):
                self._close_reservation(reservation_id, completed=False)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.persistence_error = "state_unavailable: %s" % exc
            self.mark_gap("state_unavailable")

    @staticmethod
    def _validate(state):
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported accounting schema")
        # Reject NaN/Infinity anywhere, including extension/controller state.
        json.dumps(state, allow_nan=False)
        for name in ("total", "overhead"):
            for key in _totals():
                _positive(state[name][key], name + "." + key, True)
        for key in ("logical_s", "uncertain_until", "gap_count"):
            _positive(state[key], key, True)
        for key in ("net_ah", "net_wh"):
            if not _number(state[key]):
                raise ValueError("invalid " + key)
        for key in ("capacities", "reservations", "controller_state", "references",
                    "reverse", "recovery", "buffer", "calendar_days"):
            if not isinstance(state[key], dict):
                raise ValueError("invalid " + key)
        if not isinstance(state["complete_history"], bool):
            raise ValueError("invalid history flag")
        if not isinstance(state["uncertainty_reason"], str):
            raise ValueError("invalid uncertainty reason")
        if state["wall_s"] is not None and not _number(state["wall_s"]):
            raise ValueError("invalid wall time")
        refs = state["references"]
        if refs["mode"] not in POLICY_MODES:
            raise ValueError("invalid persisted mode")
        for key in ("high_ah", "low_ah", "high_wh", "low_wh"):
            if not _number(refs[key]):
                raise ValueError("invalid directional reference")
        for key in ("target_soc", "high_soc", "low_soc"):
            if refs[key] is not None and (not _number(refs[key]) or not 0 <= refs[key] <= 100):
                raise ValueError("invalid SOC reference")
        for section, keys in (("buffer", ("credit_ah", "credit_wh")),
                              ("recovery", ("charge_ah", "discharge_ah")),
                              ("reverse", ("event_wh", "total_wh"))):
            for key in keys:
                _positive(state[section][key], key, True)
        for value in state["capacities"].values():
            _positive(value["capacity_ah"], "capacity")
            _positive(value["nominal_voltage_v"], "nominal voltage")
            for name in ("total", "overhead"):
                for key in _totals():
                    _positive(value[name][key], key, True)
        if not isinstance(state["buckets"], list):
            raise ValueError("invalid rolling buckets")
        for bucket in state["buckets"]:
            for key in ("end_s", "efc", "reverse_wh"):
                _positive(bucket[key], key, True)
            for key in ("charge_ah", "discharge_ah", "charge_wh", "discharge_wh"):
                _positive(bucket[key], key, True)
        for reservation in state["reservations"].values():
            if reservation["category"] not in OVERHEAD_CATEGORIES:
                raise ValueError("invalid reservation category")
            for key in ("remaining_efc", "remaining_wh", "remaining_reverse_wh", "created_s"):
                _positive(reservation[key], key, True)
            request = reservation["request"]
            for key in ("charge_ah", "discharge_ah", "charge_wh", "discharge_wh"):
                _positive(request[key], key)
            if request["category"] != reservation["category"] or request["capacity_version"] not in state["capacities"]:
                raise ValueError("invalid persisted reservation request")
        for day in state["calendar_days"].values():
            for section in ("total", "overhead"):
                for key in _totals():
                    _positive(day[section][key], key, True)

    def save(self):
        """Commit using fsync + same-directory rename; errors remain observable."""
        if self.path is None:
            return True
        temporary = None
        try:
            payload = json.dumps(self._state, sort_keys=True, allow_nan=False)
            directory = os.path.dirname(os.path.abspath(self.path))
            descriptor, temporary = tempfile.mkstemp(prefix=".energy-", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.persistence_error = ""
            self._checkpoint_at = self._state["logical_s"]
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.persistence_error = "state_write_failed: %s" % exc
            self.mark_gap("persistence_failure")
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def mark_gap(self, reason="invalid_sample"):
        """Invalidate discretionary credit without pretending to measure the gap."""
        self._last = None
        self._state["complete_history"] = False
        self._state["gap_count"] += 1
        self._state["uncertain_until"] = self._state["logical_s"] + WINDOW_S
        self._state["uncertainty_reason"] = str(reason)
        self._state["buffer"] = {"credit_ah": 0.0, "credit_wh": 0.0}

    def _tick(self, monotonic_s, wall_s, measurement=False):
        if not _number(monotonic_s) or not _number(wall_s):
            raise ValueError("sample clocks must be finite")
        if self._clock_mono is not None:
            elapsed = monotonic_s - self._clock_mono
            if elapsed < 0:
                if not measurement:
                    self.mark_gap("monotonic_clock_reset")
            else:
                self._state["logical_s"] += elapsed
        # CAN receipt timestamps can precede the previous controller tick. They
        # govern integration, but cannot rewind the independent budget clock.
        if not measurement or self._clock_mono is None or monotonic_s >= self._clock_mono:
            self._clock_mono = monotonic_s
        self._state["wall_s"] = wall_s
        cutoff = self._state["logical_s"] - WINDOW_S
        self._state["buckets"] = [b for b in self._state["buckets"] if b["end_s"] > cutoff]

    def observe_time(self, monotonic_s, wall_s):
        """Advance budget clocks without claiming a new battery measurement."""
        self._tick(monotonic_s, wall_s)
        self._checkpoint()

    def set_policy(self, mode, target_soc, soc=None):
        """Only a real destination change resets directional energy references.

        OFF/toggle, phase changes and transport changes retain the prior held
        references. References are integrals from REC samples, never writable by
        a policy consumer. A new target clears solar attribution, not spent wear.
        """
        if mode not in POLICY_MODES:
            raise ValueError("invalid energy policy mode")
        if not _number(target_soc) or not 0 <= target_soc <= 100:
            raise ValueError("invalid target SOC")
        if soc is not None and (not _number(soc) or not 0 <= soc <= 100):
            raise ValueError("invalid sample SOC")
        refs = self._state["references"]
        changed = refs["target_soc"] != target_soc
        if not changed and refs["mode"] == mode:
            return
        if changed:
            refs.update(high_ah=self._state["net_ah"], low_ah=self._state["net_ah"],
                        high_wh=self._state["net_wh"], low_wh=self._state["net_wh"],
                        high_soc=soc, low_soc=soc)
            self._state["buffer"] = {"credit_ah": 0.0, "credit_wh": 0.0}
        refs.update(mode=mode, target_soc=float(target_soc))
        self.save()

    def _add(self, which, interval):
        wall_s = self._state["wall_s"]
        if wall_s is not None:
            day_key = datetime.datetime.fromtimestamp(wall_s, self.calendar_timezone).date().isoformat()
            days = self._state["calendar_days"]
            if day_key in days and days[day_key]['timezone'] != str(self.calendar_timezone):
                # A configured zone change must not relabel an already measured
                # calendar day or mix two different local-day boundaries.
                day_key += '@' + str(self.calendar_timezone)
            day = days.setdefault(day_key, {"total": _totals(), "overhead": _totals(),
                                           "timezone": str(self.calendar_timezone)})
            for key, amount in interval.items():
                day[which][key] += amount
            # Daily summaries are telemetry; admission never uses calendar dates.
            for expired in sorted(days)[:-32]:
                del days[expired]
        capacity = self._state["capacities"][self.capacity_version]
        for totals in (self._state[which], capacity[which]):
            for key, amount in interval.items():
                totals[key] += amount

    def _record_cost(self, interval, reverse_wh, reservation_id=None):
        self._add("overhead", interval)
        now = self._state["logical_s"]
        # One-minute conservative buckets stay bounded and never expire a sample
        # earlier than its actual end, even for irregular sample intervals.
        bucket_end = (math.floor(now / 60.0) + 1) * 60.0
        buckets = self._state["buckets"]
        if not buckets or buckets[-1]["end_s"] != bucket_end:
            buckets.append(dict(_totals(), end_s=bucket_end, reverse_wh=0.0))
        bucket = buckets[-1]
        for key, value in interval.items():
            bucket[key] += value
        bucket["reverse_wh"] += reverse_wh
        reservation = self._state["reservations"].get(reservation_id)
        if reservation is not None:
            reservation["remaining_efc"] = max(0.0, reservation["remaining_efc"] - interval["efc"])
            reservation["remaining_wh"] = max(0.0, reservation["remaining_wh"] - interval["charge_wh"] - interval["discharge_wh"])
            reservation["remaining_reverse_wh"] = max(0.0, reservation["remaining_reverse_wh"] - reverse_wh)

    def sample(self, monotonic_s, wall_s, current_a, voltage_v, valid=True,
               soc=None, overhead_category=None, reservation_id=None,
               solar_surplus_w=None, advance_charge_reference=False):
        """Integrate successive valid raw samples, with no invented gap energy.

        ``solar_surplus_w`` is a caller-verified lower bound on PV actually
        charging the battery after all DC demand/shore contribution. Missing or
        uncertain attribution never grants buffer credit. The minimum of both
        interval endpoints bounds attributable charge. A reservation's category
        supplies explicit attribution when ``overhead_category`` is omitted.
        ``advance_charge_reference`` additionally certifies fresh, aligned solar
        evidence outside actuator settling. Both endpoints must grant it before
        CHARGE can promote its earned reference, and only by measured PV charge.
        """
        if overhead_category is not None and overhead_category not in OVERHEAD_CATEGORIES:
            raise ValueError("unknown overhead category")
        self._tick(monotonic_s, wall_s, measurement=True)
        result = dict(_totals(), valid=False, dt_s=0.0, reverse_wh=0.0)
        if not valid or not _number(current_a) or not _number(voltage_v) or voltage_v <= 0:
            self.mark_gap("invalid_sample")
            self._checkpoint()
            return result
        if soc is not None and (not _number(soc) or not 0 <= soc <= 100):
            soc = None
        if not _number(solar_surplus_w) or solar_surplus_w < 0:
            solar_surplus_w = None
        previous = self._last
        self._last = (monotonic_s, float(current_a), float(voltage_v), solar_surplus_w,
                      advance_charge_reference is True)
        if previous is None:
            self._checkpoint()
            return result
        dt = monotonic_s - previous[0]
        if dt <= 0:
            # Repeated observations must not replace the last integration anchor.
            self._last = previous
            return result
        if dt > self.max_gap_s:
            self.mark_gap("sample_gap")
            self._last = (monotonic_s, float(current_a), float(voltage_v), solar_surplus_w,
                          advance_charge_reference is True)
            self._checkpoint()
            return result
        hours = dt / 3600.0
        qa, qd = _integrate_signed(previous[1], current_a, hours)
        ec, ed = _integrate_signed(previous[1] * previous[2], current_a * voltage_v, hours)
        interval = dict(charge_ah=qa, discharge_ah=qd, charge_wh=ec,
                        discharge_wh=ed, efc=(qa + qd) / (2.0 * self.capacity_ah))
        self._add("total", interval)
        self._state["net_ah"] += qa - qd
        self._state["net_wh"] += ec - ed
        solar_wh = (min(ec, previous[3] * hours, solar_surplus_w * hours)
                    if previous[3] is not None and solar_surplus_w is not None else 0.0)
        solar_ah = min(qa, solar_wh / max(previous[2], voltage_v))
        refs = self._state["references"]
        mode = refs["mode"]
        if mode != "OFF":
            if mode == "CHARGE" and previous[4] and advance_charge_reference is True:
                gained_ah = max(0.0, min(solar_ah, self._state["net_ah"] - refs["high_ah"]))
                gained_wh = max(0.0, min(solar_wh, self._state["net_wh"] - refs["high_wh"]))
                refs["high_ah"] += gained_ah
                refs["high_wh"] += gained_wh
                if soc is not None and refs["high_soc"] is not None:
                    # SOC is corroboration/display only; an old shore burst or
                    # recalibration cannot enlarge the energy-earned increment.
                    refs["high_soc"] = max(refs["high_soc"], min(
                        soc, refs["high_soc"] + gained_ah * 100.0 / self.capacity_ah))
            elif mode in ("COMPLETE_FULL", "HOLD_FULL"):
                refs["high_ah"] = max(refs["high_ah"], self._state["net_ah"])
                refs["high_wh"] = max(refs["high_wh"], self._state["net_wh"])
                if soc is not None:
                    refs["high_soc"] = max(soc, refs["high_soc"] if refs["high_soc"] is not None else soc)
            elif mode == "DISCHARGE":
                refs["low_ah"] = min(refs["low_ah"], self._state["net_ah"])
                refs["low_wh"] = min(refs["low_wh"], self._state["net_wh"])
                if soc is not None:
                    refs["low_soc"] = min(soc, refs["low_soc"] if refs["low_soc"] is not None else soc)
        reservation = self._state["reservations"].get(reservation_id)
        category = overhead_category or (reservation["category"] if reservation else
                                         "control_error" if reservation_id else None)
        overhead, reverse_wh = self._directional_cost(
            interval, mode, category, discharge_first=previous[1] < 0)
        reverse = self._state["reverse"]
        reverse["total_wh"] += reverse_wh
        # A momentary sign wobble does not reset event credit: an explicit stable
        # recovery acknowledgement closes the event with end_reverse_event().
        reverse["event_wh"] += reverse_wh
        if overhead["efc"] or reverse_wh:
            self._record_cost(overhead, reverse_wh, reservation_id)
        buffer = self._state["buffer"]
        buffer["credit_ah"] = max(0.0, buffer["credit_ah"] - qd)
        buffer["credit_wh"] = max(0.0, buffer["credit_wh"] - ed)
        if mode == "HOLD" and not self.budget()["uncertain"] and previous[3] is not None and solar_surplus_w is not None:
            buffer["credit_wh"] += solar_wh
            buffer["credit_ah"] += solar_ah
        self._checkpoint()
        result.update(interval, valid=True, dt_s=dt, reverse_wh=reverse_wh)
        return result

    def _directional_cost(self, interval, mode, category, discharge_first):
        """Charge wrong-direction energy and its measured corrective return once.

        The reverse flag describes a controller condition, not permission to
        count all useful progress in the next sample as overhead. Within a
        zero-crossing interval, process the legs in physical order so an early
        loss can be recovered later in the same interval without stale debt.
        """
        reverse_direction = ("charge" if mode == "DISCHARGE" else "discharge"
                             if mode in ("CHARGE", "COMPLETE_FULL", "HOLD_FULL") or category == "probe"
                             else None)
        reverse_wh = interval[reverse_direction + "_wh"] if reverse_direction else 0.0
        overhead = _totals()
        recovery = self._state["recovery"]
        directions = ("discharge", "charge") if discharge_first else ("charge", "discharge")
        for direction in directions:
            ah_key, wh_key = direction + "_ah", direction + "_wh"
            measured_ah = interval[ah_key]
            recovered = min(measured_ah, recovery[ah_key])
            recovery[ah_key] -= recovered
            counted = recovered
            if direction == reverse_direction or (category == "maintenance" and direction == "charge"):
                counted = measured_ah
                # A maintenance lift's later return is still its cycle cost if
                # the user changes target or the process restarts meanwhile.
                return_key = "discharge_ah" if direction == "charge" else "charge_ah"
                recovery[return_key] += measured_ah - recovered
            overhead[ah_key] = counted
            overhead[wh_key] = interval[wh_key] * counted / measured_ah if measured_ah else 0.0
        if (category is not None and category != "reverse") or mode in ("HOLD", "MAINTENANCE"):
            overhead.update(interval)
        else:
            overhead["efc"] = (overhead["charge_ah"] + overhead["discharge_ah"]) / (2.0 * self.capacity_ah)
        return overhead, reverse_wh

    def _checkpoint(self):
        if self._state["logical_s"] - self._checkpoint_at >= self.checkpoint_s:
            self.save()

    def end_reverse_event(self):
        """REC calls only after verified sustained recovery, never on a sign tick."""
        if self._state["reverse"]["event_wh"]:
            self._state["reverse"]["event_wh"] = 0.0
            self.save()

    def budget(self):
        spent = sum(b["efc"] for b in self._state["buckets"])
        reserved = sum(r["remaining_efc"] for r in self._state["reservations"].values())
        spent_wh = sum(b["charge_wh"] + b["discharge_wh"] for b in self._state["buckets"])
        reserved_wh = sum(r["remaining_wh"] for r in self._state["reservations"].values())
        limit_wh = 2.0 * self.capacity_ah * self.nominal_voltage_v * self.max_overhead_efc
        reverse = sum(b["reverse_wh"] for b in self._state["buckets"])
        uncertain = self._state["logical_s"] < self._state["uncertain_until"] or bool(self.persistence_error)
        return {"spent_efc": spent, "reserved_efc": reserved,
                "remaining_efc": 0.0 if uncertain else max(0.0, self.max_overhead_efc - spent - reserved),
                "spent_wh": spent_wh, "reserved_wh": reserved_wh,
                "remaining_wh": 0.0 if uncertain else max(0.0, limit_wh - spent_wh - reserved_wh),
                "reverse_wh": reverse,
                "reverse_remaining_wh": 0.0 if uncertain else max(0.0, self.reverse_day_limit_wh - reverse),
                "uncertain": uncertain,
                "reason": self.persistence_error or (self._state["uncertainty_reason"] if uncertain else "")}

    def reserve(self, reservation_id, category, discharge_ah, charge_ah,
                discharge_wh, charge_wh, now_wall_s=None):
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id must be a nonempty string")
        if category not in OVERHEAD_CATEGORIES:
            raise ValueError("unknown overhead category")
        amounts = {"discharge_ah": discharge_ah, "charge_ah": charge_ah,
                   "discharge_wh": discharge_wh, "charge_wh": charge_wh}
        for key, value in amounts.items():
            _positive(value, key, True)
        if discharge_ah <= 0 or charge_ah <= 0 or discharge_wh <= 0 or charge_wh <= 0:
            raise ValueError("reserve both expected cycle legs in Ah and Wh")
        efc = (discharge_ah + charge_ah) / (2.0 * self.capacity_ah)
        existing = self._state["reservations"].get(reservation_id)
        request = dict(amounts, category=category, capacity_version=self.capacity_version)
        if existing is not None:
            return existing["request"] == request and not self.budget()["uncertain"]
        budget = self.budget()
        if efc > budget["remaining_efc"] + 1e-12:
            return False
        if charge_wh + discharge_wh > budget["remaining_wh"] + 1e-9:
            return False
        if category in ("probe", "reverse"):
            reverse_wh = max(discharge_wh, charge_wh)
            outstanding = sum(r["remaining_reverse_wh"]
                              for r in self._state["reservations"].values()
                              if r["category"] in ("probe", "reverse"))
            event_remaining = max(0.0, self.reverse_event_limit_wh - self._state["reverse"]["event_wh"])
            if (reverse_wh > event_remaining + 1e-9 or
                    reverse_wh + outstanding > budget["reverse_remaining_wh"] + 1e-9):
                return False
        self._state["reservations"][reservation_id] = dict(
            category=category, request=request, remaining_efc=efc,
            remaining_wh=charge_wh + discharge_wh,
            remaining_reverse_wh=max(charge_wh, discharge_wh) if category in ("probe", "reverse") else 0.0,
            created_s=self._state["logical_s"], wall_s=now_wall_s)
        # Refuse actuation if its reservation could not be made durable.
        return self.save()

    def _close_reservation(self, reservation_id, completed):
        reservation = self._state["reservations"].pop(reservation_id, None)
        if reservation is None:
            return False
        if not completed and (reservation["remaining_efc"] or reservation["remaining_wh"] or reservation["remaining_reverse_wh"]):
            # Conservative cost is a budget debit, not invented measured energy.
            bucket = dict(_totals(), end_s=self._state["logical_s"] + 60.0,
                          reverse_wh=0.0)
            bucket["efc"] = reservation["remaining_efc"]
            # Admission cost only; do not invent lifetime measured throughput.
            bucket["discharge_wh"] = reservation["remaining_wh"]
            bucket["reverse_wh"] = reservation["remaining_reverse_wh"]
            self._state["buckets"].append(bucket)
        return True

    def close_reservation(self, reservation_id, completed=False):
        if not self._close_reservation(reservation_id, completed):
            return False
        return self.save()

    def snapshot(self, *, compact=False):
        # Control needs counters and computed budgets, not historical buckets
        # or private controller journals. Keep the full diagnostic API/storage.
        state = ({k: v for k, v in self._state.items()
                  if k not in ('buckets', 'controller_state')} if compact else self._state)
        result = copy.deepcopy(state)
        result["budget"] = self.budget()
        result["capacity_ah"] = self.capacity_ah
        result["capacity_version"] = self.capacity_version
        result["reverse"]["event_remaining_wh"] = max(
            0.0, self.reverse_event_limit_wh - result["reverse"]["event_wh"])
        return result

