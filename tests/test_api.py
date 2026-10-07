"""Integration tests: HTTP API of both services + the alert lifecycle."""

from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)


def observation(t: float, sensor="sensor-03", freq=2437.0, power=-40.0) -> dict:
    ts = (T0 + timedelta(seconds=t)).isoformat().replace("+00:00", "Z")
    return {
        "sensor_id": sensor,
        "timestamp": ts,
        "frequency_mhz": freq,
        "bandwidth_khz": 20000,
        "power_dbm": power,
        "location": {"lat": 32.08, "lon": 34.78},
    }


def send(ingest, items):
    r = ingest.post("/api/v1/observations", json=items)
    assert r.status_code in (200, 201), r.text
    return r


def alerts(client, **params):
    return client.get("/api/v1/alerts", params=params).json()


# -- validation ------------------------------------------------------------------


def test_single_valid_observation(system):
    ingest, _, _ = system
    r = ingest.post("/api/v1/observations", json=observation(0))
    assert r.status_code == 201
    assert r.json()["accepted"] == 1


def test_invalid_single_observation_is_rejected(system):
    ingest, _, _ = system
    bad = observation(0, freq=7000)
    r = ingest.post("/api/v1/observations", json=bad)
    assert r.status_code == 422
    assert "frequency_mhz" in r.text


def test_timestamp_must_be_utc(system):
    ingest, _, _ = system
    bad = observation(0)
    bad["timestamp"] = "2026-10-06T13:15:30+03:00"
    assert ingest.post("/api/v1/observations", json=bad).status_code == 422
    bad["timestamp"] = "2026-10-06T10:15:30"  # no timezone
    assert ingest.post("/api/v1/observations", json=bad).status_code == 422


def test_missing_field_is_rejected(system):
    ingest, _, _ = system
    bad = observation(0)
    del bad["location"]
    assert ingest.post("/api/v1/observations", json=bad).status_code == 422


def test_batch_partial_accept(system):
    ingest, _, _ = system
    batch = [observation(0), observation(1, power=99), observation(2, freq=0)]
    r = ingest.post("/api/v1/observations", json=batch)
    assert r.status_code == 207
    body = r.json()
    assert body["accepted"] == 1
    assert [e["index"] for e in body["errors"]] == [1, 2]


def test_query_observations_and_sensors(system):
    ingest, _, _ = system
    send(ingest, [observation(0, "a", 100), observation(5, "b", 2437), observation(9, "a", 900)])
    r = ingest.get("/api/v1/observations", params={"sensor_id": "a", "min_freq_mhz": 500})
    assert [o["frequency_mhz"] for o in r.json()] == [900]
    r = ingest.get(
        "/api/v1/observations",
        params={"start": "2026-10-06T10:00:01Z", "end": "2026-10-06T10:00:09Z"},
    )
    assert [o["sensor_id"] for o in r.json()] == ["b"]
    sensors = ingest.get("/api/v1/sensors").json()
    assert {s["sensor_id"]: s["last_seen"] for s in sensors} == {
        "a": "2026-10-06T10:00:09Z",
        "b": "2026-10-06T10:00:05Z",
    }


# -- rules -----------------------------------------------------------------------


def test_rules_loaded_from_file(system):
    ingest, _, _ = system
    ids = {r["rule_id"] for r in ingest.get("/api/v1/rules").json()}
    assert ids == {"rule-ism-high-power", "rule-58-multi-sensor"}


def test_rule_crud(system):
    ingest, _, _ = system
    rule = {
        "rule_id": "rule-test",
        "name": "test",
        "type": "power_threshold",
        "band": {"min_mhz": 400, "max_mhz": 450},
        "threshold_dbm": -60,
        "min_duration_s": 0,
        "resolve_after_s": 10,
        "severity": "LOW",
    }
    assert ingest.post("/api/v1/rules", json=rule).status_code == 201
    assert ingest.post("/api/v1/rules", json=rule).status_code == 409
    r = ingest.put("/api/v1/rules/rule-test", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    bad = ingest.put("/api/v1/rules/rule-test", json={"band": {"min_mhz": 5, "max_mhz": 1}})
    assert bad.status_code == 422
    assert ingest.delete("/api/v1/rules/rule-test").status_code == 204
    assert ingest.delete("/api/v1/rules/rule-test").status_code == 404


def test_disabled_rule_does_not_alert(system):
    ingest, alerts_api, _ = system
    ingest.put("/api/v1/rules/rule-ism-high-power", json={"enabled": False})
    send(ingest, [observation(t) for t in range(20)])
    assert alerts(alerts_api) == []


# -- alert lifecycle -----------------------------------------------------------------


def test_short_burst_does_not_open_alert(system):
    ingest, alerts_api, _ = system
    send(ingest, [observation(t) for t in range(4)])
    assert alerts(alerts_api) == []


def test_full_lifecycle(system):
    ingest, alerts_api, _ = system

    # Persistent emitter: 15 s above threshold -> one alert, deduplicated.
    send(ingest, [observation(t) for t in range(15)])
    [a] = alerts(alerts_api)
    assert a["state"] == "OPEN"
    assert a["occurrences"] == 15
    assert a["first_seen"] == "2026-10-06T10:00:00Z"

    # Acknowledge; new matches still update it.
    acked = alerts_api.post(f"/api/v1/alerts/{a['alert_id']}/ack").json()
    assert acked["state"] == "ACKNOWLEDGED"
    send(ingest, [observation(15, sensor="sensor-07", power=-30)])
    [a] = alerts(alerts_api)
    assert a["state"] == "ACKNOWLEDGED"
    assert a["occurrences"] == 16
    assert a["peak_power_dbm"] == -30
    assert a["sensor_ids"] == ["sensor-03", "sensor-07"]

    details = alerts_api.get(f"/api/v1/alerts/{a['alert_id']}").json()
    assert len(details["observations"]) == 16

    # Emitter disappears. Background traffic keeps event time moving.
    send(ingest, [observation(t, sensor="bg", freq=900, power=-90) for t in range(20, 50)])
    [a] = alerts(alerts_api)
    assert a["state"] == "RESOLVED"
    assert a["resolution"] == "auto"

    # A late observation from before the resolution joins the old alert.
    send(ingest, [observation(14.5)])
    [a] = alerts(alerts_api)
    assert a["occurrences"] == 17

    # The emitter comes back: a new alert after min_duration_s.
    send(ingest, [observation(t) for t in range(60, 72)])
    states = sorted(x["state"] for x in alerts(alerts_api))
    assert states == ["OPEN", "RESOLVED"]


def test_manual_resolve_and_invalid_transitions(system):
    ingest, alerts_api, _ = system
    send(ingest, [observation(t) for t in range(12)])
    [a] = alerts(alerts_api)
    url = f"/api/v1/alerts/{a['alert_id']}"
    r = alerts_api.post(url + "/resolve")
    assert r.json()["state"] == "RESOLVED" and r.json()["resolution"] == "manual"
    assert alerts_api.post(url + "/ack").status_code == 409
    assert alerts_api.post(url + "/resolve").status_code == 409
    assert alerts_api.get("/api/v1/alerts/alrt-nope").status_code == 404
    # Still transmitting after the manual resolve -> new alert.
    send(ingest, [observation(12)])
    assert len(alerts(alerts_api, state="OPEN")) == 1


def test_multi_sensor_alert_and_filters(system):
    ingest, alerts_api, _ = system
    send(
        ingest,
        [
            observation(0, "s1", 5805.00, -60),
            observation(1, "s2", 5805.04, -61),
            observation(2, "s3", 5804.97, -59),
        ],
    )
    [a] = alerts(alerts_api)
    assert a["rule_id"] == "rule-58-multi-sensor"
    assert a["sensor_ids"] == ["s1", "s2", "s3"]
    assert a["occurrences"] == 3
    assert len(alerts(alerts_api, sensor_id="s2")) == 1
    assert alerts(alerts_api, sensor_id="nobody") == []
    assert alerts(alerts_api, severity="HIGH") == []
    assert len(alerts(alerts_api, min_freq_mhz=5700, max_freq_mhz=5900)) == 1


# -- ops -----------------------------------------------------------------------------


def test_health_and_metrics(system):
    ingest, alerts_api, _ = system
    h = ingest.get("/health")
    assert h.status_code == 200 and h.json()["database"] == "ok"
    send(ingest, [observation(0)])
    assert "rfam_observations_total" in ingest.get("/metrics").text
    assert "rfam_alerts" in alerts_api.get("/metrics").text
