"""Unit tests for the rule engine (no database, no broker)."""

from rfam.engine import Obs, RuleEngine
from rfam.models import MultiSensorRule, PowerThresholdRule

T0 = 1_791_000_000.0

ISM = PowerThresholdRule(
    rule_id="ism",
    name="ISM",
    type="power_threshold",
    band={"min_mhz": 2400, "max_mhz": 2483.5},
    threshold_dbm=-50,
    min_duration_s=10,
    max_gap_s=5,
    resolve_after_s=30,
    severity="HIGH",
)

MULTI = MultiSensorRule(
    rule_id="multi",
    name="multi",
    type="multi_sensor",
    band={"min_mhz": 5725, "max_mhz": 5850},
    min_sensors=3,
    window_s=10,
    resolve_after_s=30,
    severity="MEDIUM",
)


def obs(i, t, freq=2437.0, power=-40.0, sensor="s1"):
    return Obs(f"o{i}", sensor, T0 + t, freq, power, 20000)


def matches(events):
    return [e for e in events if e["type"] == "match"]


def test_short_burst_does_not_match():
    eng = RuleEngine([ISM])
    events = eng.process([obs(i, i) for i in range(5)])  # 4 seconds of signal
    assert matches(events) == []


def test_persistent_signal_matches_after_min_duration():
    eng = RuleEngine([ISM])
    events = eng.process([obs(i, i) for i in range(12)])
    m = matches(events)
    # Confirmation at t=10 carries all 11 pending observations, then t=11 alone.
    assert len(m) == 2
    assert len(m[0]["observations"]) == 11
    assert len(m[1]["observations"]) == 1


def test_two_bursts_with_a_gap_do_not_add_up():
    eng = RuleEngine([ISM])
    burst1 = [obs(i, i) for i in range(5)]  # t=0..4
    burst2 = [obs(10 + i, 20 + i) for i in range(5)]  # t=20..24, gap > max_gap_s
    assert matches(eng.process(burst1 + burst2)) == []


def test_below_threshold_or_out_of_band_ignored():
    eng = RuleEngine([ISM])
    weak = [obs(i, i, power=-80) for i in range(20)]
    out_of_band = [obs(100 + i, i, freq=900.0) for i in range(20)]
    assert matches(eng.process(weak + out_of_band)) == []


def test_out_of_order_observations_extend_pending_condition():
    eng = RuleEngine([ISM])
    # Arrives shuffled; event time still spans 0..10 s.
    order = [5, 3, 4, 0, 2, 1, 6, 8, 7, 9, 10]
    m = matches(eng.process([obs(i, t) for i, t in enumerate(order)]))
    assert len(m) == 1


def test_disabled_rule_does_not_match():
    eng = RuleEngine([ISM.model_copy(update={"enabled": False})])
    assert matches(eng.process([obs(i, i) for i in range(20)])) == []


def test_watermark_is_max_event_time():
    eng = RuleEngine([ISM])
    events = eng.process([obs(0, 50), obs(1, 10)])
    assert events[-1] == {"type": "watermark", "ts": T0 + 50}


def test_multi_sensor_needs_enough_distinct_sensors():
    eng = RuleEngine([MULTI])
    two = [obs(0, 0, 5805.00, -60, "a"), obs(1, 1, 5805.03, -60, "b")]
    assert matches(eng.process(two)) == []
    m = matches(eng.process([obs(2, 2, 5804.98, -60, "c")]))
    assert len(m) == 1
    assert {o["sensor_id"] for o in m[0]["observations"]} == {"a", "b", "c"}


def test_multi_sensor_ignores_far_frequencies_and_old_reports():
    eng = RuleEngine([MULTI])
    events = eng.process(
        [
            obs(0, 0, 5805.0, -60, "a"),
            obs(1, 1, 5806.0, -60, "b"),  # 1 MHz away: different signal
            obs(2, 30, 5805.0, -60, "c"),  # outside the 10 s window
        ]
    )
    assert matches(events) == []
