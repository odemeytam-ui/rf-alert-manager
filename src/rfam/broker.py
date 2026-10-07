"""Messaging between the two services over Redis Streams.

The ingest service publishes events with XADD; the alert service reads them with a
consumer group (XREADGROUP) and acknowledges each message only after it has been
written to the database (at-least-once delivery).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Protocol

import redis

log = logging.getLogger(__name__)

STREAM_MAXLEN = 100_000  # bounded stream: Redis memory cannot grow forever


class Publisher(Protocol):
    def publish(self, events: list[dict]) -> None: ...

    def ping(self) -> bool: ...


class RedisPublisher:
    def __init__(self, url: str, stream: str) -> None:
        self._r = redis.Redis.from_url(url)
        self._stream = stream

    def publish(self, events: list[dict]) -> None:
        if not events:
            return
        pipe = self._r.pipeline(transaction=False)
        for ev in events:
            pipe.xadd(
                self._stream, {"data": json.dumps(ev)}, maxlen=STREAM_MAXLEN, approximate=True
            )
        pipe.execute()

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False


class DirectPublisher:
    """Test helper: hands events straight to a handler, no broker involved."""

    def __init__(self, handler: Callable[[dict], None] | None = None) -> None:
        self.handler = handler
        self.sent: list[dict] = []

    def publish(self, events: list[dict]) -> None:
        self.sent.extend(events)
        if self.handler:
            for ev in events:
                self.handler(ev)

    def ping(self) -> bool:
        return True


class RedisConsumer:
    def __init__(self, url: str, stream: str, group: str, consumer: str) -> None:
        self._r = redis.Redis.from_url(url)
        self._stream, self._group, self._consumer = stream, group, consumer

    def ensure_group(self) -> None:
        try:
            self._r.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def run(self, handler: Callable[[dict], None], stop) -> None:
        """Blocking loop. First re-processes our own pending (unacked) messages
        left over from a crash, then reads new ones."""
        self.ensure_group()
        start_id = "0"
        while not stop.is_set():
            try:
                resp = self._r.xreadgroup(
                    self._group, self._consumer, {self._stream: start_id}, count=100, block=1000
                )
            except redis.RedisError:
                log.exception("redis read failed, retrying")
                stop.wait(2)
                continue
            if not resp or not resp[0][1]:
                start_id = ">"  # pending backlog done, switch to new messages
                continue
            for msg_id, fields in resp[0][1]:
                try:
                    handler(json.loads(fields[b"data"]))
                except Exception:
                    # Poison message: log and ack so it does not block the stream.
                    # (A dead-letter stream would be the production answer.)
                    log.exception("failed to process message %s", msg_id)
                self._r.xack(self._stream, self._group, msg_id)

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False
