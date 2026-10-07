"""API / message schemas (Pydantic). Validation rules from the spec live here."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Observation(BaseModel):
    """One RF observation. All fields required."""

    model_config = ConfigDict(extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=128)
    timestamp: AwareDatetime
    frequency_mhz: float = Field(ge=1, le=6000)
    bandwidth_khz: float = Field(gt=0)
    power_dbm: float = Field(ge=-150, le=30)
    location: Location

    @field_validator("timestamp")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be ISO-8601 UTC (e.g. 2026-10-06T10:15:30Z)")
        return v.astimezone(UTC)


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Band(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_mhz: float = Field(ge=1, le=6000)
    max_mhz: float = Field(ge=1, le=6000)

    @model_validator(mode="after")
    def ordered(self) -> "Band":
        if self.min_mhz >= self.max_mhz:
            raise ValueError("band.min_mhz must be lower than band.max_mhz")
        return self

    def contains(self, freq_mhz: float) -> bool:
        return self.min_mhz <= freq_mhz <= self.max_mhz


class _RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str
    band: Band
    resolve_after_s: int = Field(gt=0)
    severity: Severity
    enabled: bool = True
    # Two observations within this tolerance are "the same signal" (dedup key).
    frequency_tolerance_khz: float = Field(default=100, gt=0)


class PowerThresholdRule(_RuleBase):
    type: Literal["power_threshold"]
    threshold_dbm: float = Field(ge=-150, le=30)
    min_duration_s: int = Field(ge=0)
    # While a condition is still pending (not yet min_duration_s long), a gap longer
    # than this between matching observations resets it. This is what stops a
    # short burst followed later by another short burst from adding up.
    max_gap_s: int = Field(default=5, gt=0)


class MultiSensorRule(_RuleBase):
    type: Literal["multi_sensor"]
    min_sensors: int = Field(ge=2)
    window_s: int = Field(gt=0)
    min_power_dbm: float = Field(default=-150, ge=-150, le=30)


Rule = Annotated[PowerThresholdRule | MultiSensorRule, Field(discriminator="type")]


class AlertState(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
