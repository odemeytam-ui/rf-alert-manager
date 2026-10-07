"""Alert lifecycle: turns broker events into alert rows.

OPEN -> ACKNOWLEDGED -> RESOLVED (manual or automatic). Pure database logic; the
consumer thread and the tests both call `handle_event`.
"""

from __future__ import annotations

import secrets
import time

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session

from rfam.alerts.tables import AlertObservationRow, AlertRow, StateRow
from rfam.models import AlertState

ACTIVE = (AlertState.OPEN.value, AlertState.ACKNOWLEDGED.value)

EVENTS = Counter("rfam_events_consumed_total", "Events consumed", ["type"])
OPENED = Counter("rfam_alerts_opened_total", "Alerts opened", ["rule_id", "severity"])
RESOLVED = Counter("rfam_alerts_resolved_total", "Alerts resolved", ["resolution"])
LATE = Counter("rfam_late_observations_total", "Observations attached to a resolved alert")


def new_alert_id() -> str:
    return "alrt-" + secrets.token_hex(4)


def get_watermark(s: Session) -> float:
    row = s.get(StateRow, "watermark")
    return row.value if row else 0.0


def handle_event(s: Session, ev: dict) -> None:
    EVENTS.labels(ev.get("type", "unknown")).inc()
    if ev["type"] == "match":
        _handle_match(s, ev)
    elif ev["type"] == "watermark":
        _advance_watermark(s, float(ev["ts"]))
    s.commit()


def _handle_match(s: Session, ev: dict) -> None:
    for obs in ev["observations"]:
        active = s.scalars(
            select(AlertRow)
            .where(
                AlertRow.rule_id == ev["rule_id"],
                AlertRow.signal_key == ev["signal_key"],
                AlertRow.state.in_(ACTIVE),
            )
            .with_for_update()
        ).first()
        if active is not None:
            _attach(s, active, obs)
            continue

        last_resolved = s.scalars(
            select(AlertRow)
            .where(
                AlertRow.rule_id == ev["rule_id"],
                AlertRow.signal_key == ev["signal_key"],
                AlertRow.state == AlertState.RESOLVED.value,
            )
            .order_by(AlertRow.resolved_event_time.desc())
        ).first()
        if last_resolved is not None and obs["ts"] <= (last_resolved.resolved_event_time or 0):
            # Late / out-of-order: it happened before the alert was resolved, so it
            # belongs to that alert. Do not reopen, do not open a new one.
            if _attach(s, last_resolved, obs):
                LATE.inc()
            continue

        alert = AlertRow(
            alert_id=new_alert_id(),
            rule_id=ev["rule_id"],
            rule_type=ev["rule_type"],
            signal_key=ev["signal_key"],
            state=AlertState.OPEN.value,
            severity=ev["severity"],
            frequency_mhz=obs["frequency_mhz"],
            sensor_ids=[],
            first_seen=obs["ts"],
            last_seen=obs["ts"],
            occurrences=0,
            peak_power_dbm=obs["power_dbm"],
            resolve_after_s=ev["resolve_after_s"],
            created_at=time.time(),
        )
        s.add(alert)
        s.flush()
        _attach(s, alert, obs)
        OPENED.labels(alert.rule_id, alert.severity).inc()


def _attach(s: Session, alert: AlertRow, obs: dict) -> bool:
    """Add one observation to an alert. Idempotent: redelivered messages and the
    overlapping groups of multi_sensor matches are only counted once."""
    if s.get(AlertObservationRow, (alert.alert_id, obs["id"])) is not None:
        return False
    s.add(
        AlertObservationRow(
            alert_id=alert.alert_id,
            observation_id=obs["id"],
            sensor_id=obs["sensor_id"],
            ts=obs["ts"],
            frequency_mhz=obs["frequency_mhz"],
            power_dbm=obs["power_dbm"],
            bandwidth_khz=obs["bandwidth_khz"],
        )
    )
    alert.occurrences += 1
    alert.first_seen = min(alert.first_seen, obs["ts"])
    alert.last_seen = max(alert.last_seen, obs["ts"])
    alert.peak_power_dbm = max(alert.peak_power_dbm, obs["power_dbm"])
    if obs["sensor_id"] not in alert.sensor_ids:
        alert.sensor_ids = sorted([*alert.sensor_ids, obs["sensor_id"]])
    return True


def _advance_watermark(s: Session, ts: float) -> None:
    row = s.get(StateRow, "watermark")
    if row is None:
        row = StateRow(key="watermark", value=ts)
        s.add(row)
    elif ts > row.value:
        row.value = ts
    wm = row.value
    expired = s.scalars(
        select(AlertRow)
        .where(AlertRow.state.in_(ACTIVE), AlertRow.last_seen + AlertRow.resolve_after_s < wm)
        .with_for_update()
    ).all()
    for alert in expired:
        alert.state = AlertState.RESOLVED.value
        alert.resolution = "auto"
        alert.resolved_at = time.time()
        alert.resolved_event_time = wm
        RESOLVED.labels("auto").inc()
