"""Alert service: consumes rule matches from the broker and manages alerts.

Run: uvicorn --factory rfam.alerts.app:build
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from rfam.alerts.processor import RESOLVED, get_watermark, handle_event
from rfam.alerts.tables import AlertObservationRow, AlertRow, Base
from rfam.broker import RedisConsumer
from rfam.config import get_settings
from rfam.db import db_ok, make_engine, session_factory
from rfam.models import AlertState, Severity
from rfam.timeutil import iso

log = logging.getLogger("rfam.alerts")

ALERTS_BY_STATE = Gauge("rfam_alerts", "Alerts by state", ["state"])
WATERMARK = Gauge("rfam_event_time_watermark_seconds", "Event-time watermark (epoch)")


def alert_json(a: AlertRow) -> dict:
    return {
        "alert_id": a.alert_id,
        "rule_id": a.rule_id,
        "rule_type": a.rule_type,
        "state": a.state,
        "severity": a.severity,
        "frequency_mhz": a.frequency_mhz,
        "sensor_ids": a.sensor_ids,
        "first_seen": iso(a.first_seen),
        "last_seen": iso(a.last_seen),
        "occurrences": a.occurrences,
        "peak_power_dbm": a.peak_power_dbm,
        "acknowledged_at": iso(a.acknowledged_at),
        "resolved_at": iso(a.resolved_at),
        "resolution": a.resolution,
    }


def create_app(engine: Engine | None = None, start_consumer: bool = True) -> FastAPI:
    settings = get_settings()
    engine = engine or make_engine(settings.database_url)
    Sessions = session_factory(engine)
    consumer = RedisConsumer(
        settings.redis_url, settings.stream_name, settings.consumer_group, socket.gethostname()
    )
    stop = threading.Event()
    worker: dict[str, threading.Thread | None] = {"t": None}

    def on_event(ev: dict) -> None:
        with Sessions() as s:
            handle_event(s, ev)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        if start_consumer:
            t = threading.Thread(
                target=consumer.run, args=(on_event, stop), name="consumer", daemon=True
            )
            t.start()
            worker["t"] = t
        log.info("alert service started")
        yield
        stop.set()
        if worker["t"]:
            worker["t"].join(timeout=5)

    app = FastAPI(title="RF Alert Manager - alert service", lifespan=lifespan)
    app.state.on_event = on_event  # used by tests to inject events without Redis

    def session():
        with Sessions() as s:
            yield s

    def get_alert(s: Session, alert_id: str) -> AlertRow:
        a = s.scalars(
            select(AlertRow).where(AlertRow.alert_id == alert_id).with_for_update()
        ).first()
        if a is None:
            raise HTTPException(404, "alert not found")
        return a

    @app.get("/api/v1/alerts")
    def list_alerts(
        state: AlertState | None = None,
        severity: Severity | None = None,
        sensor_id: str | None = None,
        min_freq_mhz: float | None = None,
        max_freq_mhz: float | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        s: Session = Depends(session),
    ):
        q = select(AlertRow)
        if state:
            q = q.where(AlertRow.state == state.value)
        if severity:
            q = q.where(AlertRow.severity == severity.value)
        if min_freq_mhz is not None:
            q = q.where(AlertRow.frequency_mhz >= min_freq_mhz)
        if max_freq_mhz is not None:
            q = q.where(AlertRow.frequency_mhz <= max_freq_mhz)
        if sensor_id:
            q = q.where(
                AlertRow.alert_id.in_(
                    select(AlertObservationRow.alert_id).where(
                        AlertObservationRow.sensor_id == sensor_id
                    )
                )
            )
        q = q.order_by(AlertRow.last_seen.desc()).limit(limit).offset(offset)
        return [alert_json(a) for a in s.scalars(q)]

    @app.get("/api/v1/alerts/{alert_id}")
    def alert_details(
        alert_id: str,
        max_observations: int = Query(500, ge=1, le=5000),
        s: Session = Depends(session),
    ):
        a = s.get(AlertRow, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        obs = s.scalars(
            select(AlertObservationRow)
            .where(AlertObservationRow.alert_id == alert_id)
            .order_by(AlertObservationRow.ts)
            .limit(max_observations)
        )
        body = alert_json(a)
        body["observations"] = [
            {
                "id": o.observation_id,
                "sensor_id": o.sensor_id,
                "timestamp": iso(o.ts),
                "frequency_mhz": o.frequency_mhz,
                "power_dbm": o.power_dbm,
                "bandwidth_khz": o.bandwidth_khz,
            }
            for o in obs
        ]
        return body

    @app.post("/api/v1/alerts/{alert_id}/ack")
    def ack(alert_id: str, s: Session = Depends(session)):
        a = get_alert(s, alert_id)
        if a.state == AlertState.RESOLVED.value:
            raise HTTPException(409, "alert is already resolved")
        if a.state == AlertState.OPEN.value:
            a.state = AlertState.ACKNOWLEDGED.value
            a.acknowledged_at = time.time()
            s.commit()
        return alert_json(a)

    @app.post("/api/v1/alerts/{alert_id}/resolve")
    def resolve(alert_id: str, s: Session = Depends(session)):
        a = get_alert(s, alert_id)
        if a.state == AlertState.RESOLVED.value:
            raise HTTPException(409, "alert is already resolved")
        a.state = AlertState.RESOLVED.value
        a.resolution = "manual"
        a.resolved_at = time.time()
        a.resolved_event_time = max(get_watermark(s), a.last_seen)
        s.commit()
        RESOLVED.labels("manual").inc()
        return alert_json(a)

    @app.get("/health")
    def health():
        db, broker = db_ok(engine), consumer.ping()
        alive = (not start_consumer) or bool(worker["t"] and worker["t"].is_alive())
        ok = db and broker and alive
        body = {
            "service": "alerts",
            "status": "ok" if ok else "unhealthy",
            "database": "ok" if db else "down",
            "broker": "ok" if broker else "down",
            "consumer": "running" if alive else "stopped",
        }
        return JSONResponse(status_code=200 if ok else 503, content=body)

    @app.get("/metrics")
    def metrics(s: Session = Depends(session)):
        counts = dict(
            s.execute(select(AlertRow.state, func.count()).group_by(AlertRow.state)).all()
        )
        for st in AlertState:
            ALERTS_BY_STATE.labels(st.value).set(counts.get(st.value, 0))
        WATERMARK.set(get_watermark(s))
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def build() -> FastAPI:
    """Entry point for `uvicorn --factory rfam.alerts.app:build`."""
    logging.basicConfig(
        level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return create_app()
