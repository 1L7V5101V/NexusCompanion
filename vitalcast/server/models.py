"""Data models for VitalCast health metrics and alerts.

All models are frozen dataclasses — the canonical choice for internal
value objects that carry no I/O logic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HealthSample:
    """A single health metric reading from VitalCast iOS app.

    The *value* field holds either a numeric measurement (heartRate, steps,
    bodyWeight, activeEnergy) or a structured mapping (sleep: state, inBed,
    asleep).  Unknown metric types are stored verbatim for forward
    compatibility.
    """

    type: str
    value: float | dict[str, str | float]
    unit: str
    date: str  # ISO8601 timestamp


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A health alert event triggered by VitalCast (e.g. high heart rate)."""

    id: str
    type: str
    value: float
    unit: str
    timestamp: str  # ISO8601
    message: str
