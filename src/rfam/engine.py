"""Rule engine: pure Python, no I/O, so it is easy to unit test.

Input: observations (in any order). Output: events to publish on the broker.

Event types
-----------
match      -- a rule condition is (still) true for a signal; carries the observations
              that contributed. The alert service turns these into alerts.
watermark  -- the highest event time seen so far. The alert service uses it as its
              clock for auto-resolve, so the whole system runs on event time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from rfam.models import MultiSensorRule, PowerThresholdRule

RuleT = PowerThresholdRule | MultiSensorRule


@dataclass(frozen=True)
class Obs:
    id: str
    sensor_id: str
    ts: float  # event time, epoch seconds UTC
    frequency_mhz: float
    power_dbm: float
    bandwidth_khz: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sensor_id": self.sensor_id,
            "ts": self.ts,
            "frequency_mhz": self.frequency_mhz,
            "power_dbm": self.power_dbm,
            "bandwidth_khz": self.bandwidth_khz,
        }


@dataclass
class _Pending:
    first_ts: float
    last_ts: float
    confirmed: bool = False
    pending: list[Obs] = field(default_factory=list)


MAX_PENDING = 1000  # bound memory for one pending condition


def signal_key(rule: RuleT, freq_mhz: float) -> int:
    """Frequencies within the rule's tolerance map to the same bucket = same signal."""
    return int(round(freq_mhz * 1000 / rule.frequency_tolerance_khz))


class RuleEngine:
    def __init__(self, rules: list[RuleT] | None = None) -> None:
        self._rules: dict[str, RuleT] = {}
        self._power: dict[tuple[str, int], _Pending] = {}
        self._multi: dict[str, deque[Obs]] = {}
        self.watermark: float = 0.0
        self.set_rules(rules or [])

    # -- rule management ----------------------------------------------------
    def set_rules(self, rules: list[RuleT]) -> None:
        new = {r.rule_id: r for r in rules}
        # Drop state of rules that were removed or changed, so an edited
        # threshold starts from a clean slate.
        for key in list(self._power):
            if new.get(key[0]) != self._rules.get(key[0]):
                del self._power[key]
        for rid in list(self._multi):
            if new.get(rid) != self._rules.get(rid):
                del self._multi[rid]
        self._rules = new

    # -- evaluation -----------------------------------------------------------
    def process(self, observations: list[Obs]) -> list[dict]:
        events: list[dict] = []
        for obs in observations:
            for rule in self._rules.values():
                if not rule.enabled or not rule.band.contains(obs.frequency_mhz):
                    continue
                if isinstance(rule, PowerThresholdRule):
                    ev = self._power_threshold(rule, obs)
                else:
                    ev = self._multi_sensor(rule, obs)
                if ev:
                    events.append(ev)
            self.watermark = max(self.watermark, obs.ts)
        if observations:
            events.append({"type": "watermark", "ts": self.watermark})
            self._gc()
        return events

    def _match(self, rule: RuleT, freq: float, obs: list[Obs]) -> dict:
        return {
            "type": "match",
            "rule_id": rule.rule_id,
            "rule_type": rule.type,
            "severity": rule.severity.value,
            "signal_key": signal_key(rule, freq),
            "frequency_mhz": freq,
            "resolve_after_s": rule.resolve_after_s,
            "observations": [o.to_dict() for o in obs],
        }

    def _power_threshold(self, rule: PowerThresholdRule, obs: Obs) -> dict | None:
        if obs.power_dbm <= rule.threshold_dbm:
            return None
        key = (rule.rule_id, signal_key(rule, obs.frequency_mhz))
        st = self._power.get(key)
        if st is not None:
            # Once confirmed, the condition lives as long as the alert would.
            gap_limit = rule.resolve_after_s if st.confirmed else rule.max_gap_s
            if obs.ts - st.last_ts > gap_limit:
                st = None  # condition was interrupted: start over
            elif obs.ts < st.first_ts:
                if st.first_ts - obs.ts > gap_limit and not st.confirmed:
                    return None  # very late and unrelated to the current condition
                st.first_ts = min(st.first_ts, obs.ts)
        if st is None:
            st = _Pending(first_ts=obs.ts, last_ts=obs.ts)
            self._power[key] = st
        st.last_ts = max(st.last_ts, obs.ts)

        if st.confirmed:
            return self._match(rule, obs.frequency_mhz, [obs])
        if len(st.pending) < MAX_PENDING:
            st.pending.append(obs)
        if st.last_ts - st.first_ts >= rule.min_duration_s:
            st.confirmed = True
            contributing, st.pending = st.pending, []
            return self._match(rule, contributing[0].frequency_mhz, contributing)
        return None

    def _multi_sensor(self, rule: MultiSensorRule, obs: Obs) -> dict | None:
        if obs.power_dbm < rule.min_power_dbm:
            return None
        window = self._multi.setdefault(rule.rule_id, deque())
        window.append(obs)
        tol_mhz = rule.frequency_tolerance_khz / 1000
        group = [
            o
            for o in window
            if abs(o.ts - obs.ts) <= rule.window_s
            and abs(o.frequency_mhz - obs.frequency_mhz) <= tol_mhz
        ]
        if len({o.sensor_id for o in group}) >= rule.min_sensors:
            return self._match(rule, obs.frequency_mhz, group)
        return None

    def _gc(self) -> None:
        """Forget state that can no longer matter (bounded memory)."""
        for key, st in list(self._power.items()):
            rule = self._rules.get(key[0])
            horizon = rule.resolve_after_s if rule else 0
            if self.watermark - st.last_ts > horizon + 60:
                del self._power[key]
        for rid, window in self._multi.items():
            rule = self._rules.get(rid)
            keep = (rule.window_s if rule else 0) * 2
            while window and self.watermark - window[0].ts > keep:
                window.popleft()
