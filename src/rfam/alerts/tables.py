"""Tables owned by the alert service.

Event times are stored as epoch seconds (float). That keeps the auto-resolve query
(`last_seen + resolve_after_s < watermark`) a plain arithmetic comparison that works
the same on PostgreSQL and SQLite.
"""

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AlertRow(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(128))
    rule_type: Mapped[str] = mapped_column(String(32))
    signal_key: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(16))
    frequency_mhz: Mapped[float] = mapped_column(Float)
    sensor_ids: Mapped[list] = mapped_column(JSON)
    first_seen: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)
    occurrences: Mapped[int] = mapped_column(Integer)
    peak_power_dbm: Mapped[float] = mapped_column(Float)
    resolve_after_s: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)  # wall clock
    acknowledged_at: Mapped[float | None] = mapped_column(Float, nullable=True)  # wall clock
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)  # wall clock
    # Event-time watermark at the moment of resolution. Observations older than this
    # that arrive later are "late" and attach to this alert instead of opening a new one.
    resolved_event_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # auto|manual

    __table_args__ = (
        Index("ix_alert_rule_signal_state", "rule_id", "signal_key", "state"),
        Index("ix_alert_state", "state"),
    )


class AlertObservationRow(Base):
    """Contributing observations (a copy, so the alert service owns its own data)."""

    __tablename__ = "alert_observations"

    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.alert_id"), primary_key=True)
    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sensor_id: Mapped[str] = mapped_column(String(128))
    ts: Mapped[float] = mapped_column(Float)
    frequency_mhz: Mapped[float] = mapped_column(Float)
    power_dbm: Mapped[float] = mapped_column(Float)
    bandwidth_khz: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_alert_obs_sensor", "sensor_id"),)


class StateRow(Base):
    """Small key/value table; holds the event-time watermark across restarts."""

    __tablename__ = "alert_service_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
