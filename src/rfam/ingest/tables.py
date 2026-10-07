"""Tables owned by the ingest service."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ObservationRow(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sensor_id: Mapped[str] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    frequency_mhz: Mapped[float] = mapped_column(Float)
    bandwidth_khz: Mapped[float] = mapped_column(Float)
    power_dbm: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_obs_sensor_ts", "sensor_id", "timestamp"),
        Index("ix_obs_ts", "timestamp"),
        Index("ix_obs_freq", "frequency_mhz"),
    )


class SensorRow(Base):
    __tablename__ = "sensors"

    sensor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # event time
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # arrival
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)


class RuleRow(Base):
    __tablename__ = "rules"

    rule_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
