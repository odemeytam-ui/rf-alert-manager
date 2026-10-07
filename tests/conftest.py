"""Test setup: both services on one in-memory SQLite DB, wired together without
Redis. The ingest service's publisher hands events straight to the alert
service's handler, so the tests cover the full flow ingest -> rules -> alerts."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rfam.alerts.app import create_app as create_alerts
from rfam.broker import DirectPublisher
from rfam.db import make_engine
from rfam.ingest.app import create_app as create_ingest

RULES = str(Path(__file__).parent.parent / "config" / "rules.yaml")


@pytest.fixture
def system():
    engine = make_engine("sqlite:///:memory:")
    alerts_app = create_alerts(engine=engine, start_consumer=False)
    publisher = DirectPublisher()
    ingest_app = create_ingest(engine=engine, publisher=publisher, rules_file=RULES)
    with TestClient(alerts_app) as alerts, TestClient(ingest_app) as ingest:
        publisher.handler = alerts_app.state.on_event
        yield ingest, alerts, publisher
