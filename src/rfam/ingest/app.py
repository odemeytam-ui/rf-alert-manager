"""Ingest service: observations, sensors, rules, rule evaluation.

Run: uvicorn rfam.ingest.app:app
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from rfam.broker import Publisher, RedisPublisher
from rfam.config import get_settings
from rfam.db import db_ok, make_engine, session_factory
from rfam.engine import Obs, RuleEngine
from rfam.ingest.tables import Base, ObservationRow, RuleRow, SensorRow
from rfam.models import Observation, Rule
from rfam.timeutil import iso, now_utc, to_epoch

log = logging.getLogger("rfam.ingest")

MAX_BATCH = 1000

OBS_TOTAL = Counter("rfam_observations_total", "Observations received", ["result"])
EVENTS_TOTAL = Counter("rfam_events_published_total", "Events published", ["type"])
PUBLISH_ERRORS = Counter("rfam_publish_errors_total", "Failed publishes to the broker")
INGEST_SECONDS = Histogram("rfam_ingest_request_seconds", "POST /observations latency")

obs_adapter = TypeAdapter(Observation)
rule_adapter = TypeAdapter(Rule)


def _errors(e: ValidationError) -> list[dict]:
    return json.loads(e.json(include_url=False))


def load_rules_file(path: str) -> list[dict]:
    with open(path) as f:
        if path.endswith((".yaml", ".yml")):
            doc = yaml.safe_load(f)
        else:
            doc = json.load(f)
    return doc.get("rules", []) if isinstance(doc, dict) else doc


def create_app(
    engine: Engine | None = None,
    publisher: Publisher | None = None,
    rules_file: str | None = None,
) -> FastAPI:
    settings = get_settings()
    engine = engine or make_engine(settings.database_url)
    publisher = publisher or RedisPublisher(settings.redis_url, settings.stream_name)
    rules_file = rules_file if rules_file is not None else settings.rules_file
    Sessions = session_factory(engine)
    rule_engine = RuleEngine()
    # One writer at a time per instance: keeps the in-memory rule state, the sensor
    # upserts and the order of published events consistent. See DECISIONS.md.
    lock = threading.Lock()

    def reload_rules(s: Session) -> None:
        rows = s.scalars(select(RuleRow)).all()
        rule_engine.set_rules([rule_adapter.validate_python(r.data) for r in rows])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        with Sessions() as s:
            if rules_file:
                for raw in load_rules_file(rules_file):
                    rule = rule_adapter.validate_python(raw)
                    # Only insert missing rules, so changes made through the API
                    # survive a restart.
                    if s.get(RuleRow, rule.rule_id) is None:
                        s.add(
                            RuleRow(
                                rule_id=rule.rule_id,
                                data=rule.model_dump(mode="json"),
                                updated_at=now_utc(),
                            )
                        )
                s.commit()
            reload_rules(s)
        log.info("ingest service started")
        yield

    app = FastAPI(title="RF Alert Manager - ingest service", lifespan=lifespan)

    def session():
        with Sessions() as s:
            yield s

    # -- observations ---------------------------------------------------------
    @app.post("/api/v1/observations", status_code=201)
    def post_observations(payload: Any = Body(...), s: Session = Depends(session)):
        started = time.perf_counter()
        is_batch = isinstance(payload, list)
        items = payload if is_batch else [payload]
        if not items:
            raise HTTPException(422, "empty batch")
        if len(items) > MAX_BATCH:
            raise HTTPException(413, f"batch larger than {MAX_BATCH} items")

        valid: list[Observation] = []
        rejected: list[dict] = []
        for i, raw in enumerate(items):
            try:
                valid.append(obs_adapter.validate_python(raw))
            except ValidationError as e:
                rejected.append({"index": i, "errors": _errors(e)})
        OBS_TOTAL.labels("rejected").inc(len(rejected))

        if not valid:
            detail = rejected if is_batch else rejected[0]["errors"]
            return JSONResponse(status_code=422, content={"detail": detail})

        received = now_utc()
        ids: list[str] = []
        engine_input: list[Obs] = []
        with lock:
            for o in valid:
                oid = str(uuid.uuid4())
                ids.append(oid)
                s.add(
                    ObservationRow(
                        id=oid,
                        sensor_id=o.sensor_id,
                        timestamp=o.timestamp,
                        frequency_mhz=o.frequency_mhz,
                        bandwidth_khz=o.bandwidth_khz,
                        power_dbm=o.power_dbm,
                        lat=o.location.lat,
                        lon=o.location.lon,
                        received_at=received,
                    )
                )
                sensor = s.get(SensorRow, o.sensor_id)
                if sensor is None:
                    sensor = SensorRow(
                        sensor_id=o.sensor_id,
                        last_seen=o.timestamp,
                        last_received_at=received,
                        lat=o.location.lat,
                        lon=o.location.lon,
                        observation_count=0,
                    )
                    s.add(sensor)
                    s.flush()
                if to_epoch(o.timestamp) >= to_epoch(sensor.last_seen):
                    sensor.last_seen = o.timestamp
                    sensor.lat, sensor.lon = o.location.lat, o.location.lon
                sensor.last_received_at = received
                sensor.observation_count += 1
                engine_input.append(
                    Obs(
                        id=oid,
                        sensor_id=o.sensor_id,
                        ts=to_epoch(o.timestamp),
                        frequency_mhz=o.frequency_mhz,
                        power_dbm=o.power_dbm,
                        bandwidth_khz=o.bandwidth_khz,
                    )
                )
            s.commit()
            events = rule_engine.process(engine_input)
            try:
                publisher.publish(events)
                for ev in events:
                    EVENTS_TOTAL.labels(ev["type"]).inc()
            except Exception:
                # Observations are stored; only the alert evaluation is lost.
                # An outbox table would fix this (see DECISIONS.md).
                PUBLISH_ERRORS.inc()
                log.exception("publishing %d events failed", len(events))

        OBS_TOTAL.labels("accepted").inc(len(valid))
        INGEST_SECONDS.observe(time.perf_counter() - started)
        body = {"accepted": len(valid), "rejected": len(rejected), "ids": ids}
        if rejected:
            body["errors"] = rejected
            return JSONResponse(status_code=207, content=body)
        return body

    @app.get("/api/v1/observations")
    def get_observations(
        sensor_id: str | None = None,
        min_freq_mhz: float | None = None,
        max_freq_mhz: float | None = None,
        start: datetime | None = Query(None, description="ISO-8601, inclusive"),
        end: datetime | None = Query(None, description="ISO-8601, exclusive"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        s: Session = Depends(session),
    ):
        q = select(ObservationRow)
        if sensor_id:
            q = q.where(ObservationRow.sensor_id == sensor_id)
        if min_freq_mhz is not None:
            q = q.where(ObservationRow.frequency_mhz >= min_freq_mhz)
        if max_freq_mhz is not None:
            q = q.where(ObservationRow.frequency_mhz <= max_freq_mhz)
        if start:
            q = q.where(ObservationRow.timestamp >= start)
        if end:
            q = q.where(ObservationRow.timestamp < end)
        q = q.order_by(ObservationRow.timestamp.desc()).limit(limit).offset(offset)
        return [
            {
                "id": r.id,
                "sensor_id": r.sensor_id,
                "timestamp": iso(r.timestamp),
                "frequency_mhz": r.frequency_mhz,
                "bandwidth_khz": r.bandwidth_khz,
                "power_dbm": r.power_dbm,
                "location": {"lat": r.lat, "lon": r.lon},
            }
            for r in s.scalars(q)
        ]

    @app.get("/api/v1/sensors")
    def get_sensors(s: Session = Depends(session)):
        rows = s.scalars(select(SensorRow).order_by(SensorRow.sensor_id))
        return [
            {
                "sensor_id": r.sensor_id,
                "last_seen": iso(r.last_seen),
                "last_received_at": iso(r.last_received_at),
                "location": {"lat": r.lat, "lon": r.lon},
                "observation_count": r.observation_count,
            }
            for r in rows
        ]

    # -- rules ------------------------------------------------------------------
    @app.get("/api/v1/rules")
    def list_rules(s: Session = Depends(session)):
        return [r.data for r in s.scalars(select(RuleRow).order_by(RuleRow.rule_id))]

    @app.post("/api/v1/rules", status_code=201)
    def create_rule(payload: dict = Body(...), s: Session = Depends(session)):
        try:
            rule = rule_adapter.validate_python(payload)
        except ValidationError as e:
            return JSONResponse(status_code=422, content={"detail": _errors(e)})
        if s.get(RuleRow, rule.rule_id):
            raise HTTPException(409, f"rule {rule.rule_id} already exists")
        data = rule.model_dump(mode="json")
        s.add(RuleRow(rule_id=rule.rule_id, data=data, updated_at=now_utc()))
        s.commit()
        with lock:
            reload_rules(s)
        return data

    @app.put("/api/v1/rules/{rule_id}")
    def update_rule(rule_id: str, payload: dict = Body(...), s: Session = Depends(session)):
        row = s.get(RuleRow, rule_id)
        if row is None:
            raise HTTPException(404, "rule not found")
        if payload.get("rule_id", rule_id) != rule_id:
            raise HTTPException(422, "rule_id in body does not match the URL")
        # Fields not sent keep their current value, so {"enabled": false} works.
        merged = {**row.data, **payload, "rule_id": rule_id}
        try:
            rule = rule_adapter.validate_python(merged)
        except ValidationError as e:
            return JSONResponse(status_code=422, content={"detail": _errors(e)})
        row.data = rule.model_dump(mode="json")
        row.updated_at = now_utc()
        s.commit()
        with lock:
            reload_rules(s)
        return row.data

    @app.delete("/api/v1/rules/{rule_id}", status_code=204)
    def delete_rule(rule_id: str, s: Session = Depends(session)):
        row = s.get(RuleRow, rule_id)
        if row is None:
            raise HTTPException(404, "rule not found")
        s.delete(row)
        s.commit()
        with lock:
            reload_rules(s)
        return Response(status_code=204)

    # -- ops ---------------------------------------------------------------------
    @app.get("/health")
    def health():
        db, broker = db_ok(engine), publisher.ping()
        body = {
            "service": "ingest",
            "status": "ok" if db and broker else "unhealthy",
            "database": "ok" if db else "down",
            "broker": "ok" if broker else "down",
        }
        return JSONResponse(status_code=200 if db and broker else 503, content=body)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def build() -> FastAPI:
    """Entry point for `uvicorn --factory rfam.ingest.app:build`."""
    logging.basicConfig(
        level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return create_app()
