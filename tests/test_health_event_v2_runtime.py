from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _import_health_event_v2():
    module_dir = Path(__file__).resolve().parents[1] / "scripts" / "fitbit-monitor"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    import health_event_v2  # type: ignore

    return health_event_v2


def test_update_persists_metrics_into_pending(tmp_path):
    mod = _import_health_event_v2()
    runtime = mod.HealthEventV2Runtime(state_path=tmp_path / "health_event_v2_state.json")

    class _FakeEngine:
        def process(self, rows):
            return [
                mod.V2Event(
                    event_id="evt-001",
                    type="recovery_debt",
                    severity="high",
                    confidence=0.9,
                    message="测试事件",
                    created_at="2099-01-01 00:00:00",
                    metrics={"sleep_hours": 5.1, "debt_hours": 2.3},
                )
            ]

    original_engine = mod.HealthEventV2Engine
    try:
        mod.HealthEventV2Engine = _FakeEngine
        runtime.update(
            log_entry={"poll_time": "2099-01-01 00:00:00"},
            history=[{"poll_time": "2099-01-01 00:00:00"}],
        )
        events = runtime.get_pending_events()
    finally:
        mod.HealthEventV2Engine = original_engine

    assert len(events) == 1
    assert events[0]["id"] == "evt-001"
    assert events[0]["metrics"] == {"sleep_hours": 5.1, "debt_hours": 2.3}


def test_spo2_no_longer_emits_persistent_low_oxygen():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    rows = [
        {
            "poll_time": f"2099-01-01 12:{minute:02d}:00",
            "state": "awake",
            "spo2": 88.5,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        }
        for minute in (0, 5, 10, 15, 20, 25)
    ]

    events = engine.process(rows)

    assert not any(e.type == "persistent_low_oxygen" for e in events)


def test_uncertain_counts_as_sleeping_for_recovery_debt_daily_aggregation():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    engine.ingest_row(
        {
            "poll_time": "2099-01-01 02:00:00",
            "state": "uncertain",
            "spo2": 91.2,
            "spo2_time": "07:10:00",
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        }
    )

    day = engine._daily["2099-01-01"]
    assert day["sleeping_polls"] == 1
    assert day["night_spo2"] == [91.2]


def test_spo2_uses_spo2_time_and_deduplicates_same_sample():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    engine.ingest_row(
        {
            "poll_time": "2099-01-01 07:00:00",
            "state": "sleeping",
            "data_lag_min": 1,
            "signals": {"zero_steps_count": 20},
        }
    )
    engine.ingest_row(
        {
            "poll_time": "2099-01-01 08:00:00",
            "state": "awake",
            "data_lag_min": 1,
            "signals": {"zero_steps_count": 0},
        }
    )
    engine.ingest_row(
        {
            "poll_time": "2099-01-01 09:00:00",
            "state": "awake",
            "spo2": 90.1,
            "spo2_time": "08:10:00",
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 0},
        }
    )
    engine.ingest_row(
        {
            "poll_time": "2099-01-01 09:05:00",
            "state": "awake",
            "spo2": 90.1,
            "spo2_time": "08:10:00",
            "data_lag_min": 10,
            "signals": {"zero_steps_count": 0},
        }
    )
    engine.ingest_row(
        {
            "poll_time": "2099-01-01 09:10:00",
            "state": "awake",
            "spo2": 89.8,
            "spo2_time": "08:15:00",
            "data_lag_min": 15,
            "signals": {"zero_steps_count": 0},
        }
    )

    day = engine._daily["2099-01-01"]
    assert day["night_spo2"] == pytest.approx([89.95])


def test_stale_sleep_spo2_does_not_trigger_cardio_respiratory_strain():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    for minute in range(30):
        engine.ingest_row(
            {
                "poll_time": f"2099-01-01 10:{minute:02d}:00",
                "state": "awake",
                "heart_rate": 88.0,
                "data_lag_min": 5,
                "signals": {"zero_steps_count": 20},
            }
        )

    engine.ingest_row(
        {
            "poll_time": "2099-01-02 07:00:00",
            "state": "sleeping",
            "data_lag_min": 1,
            "signals": {"zero_steps_count": 20},
        }
    )
    engine.ingest_row(
        {
            "poll_time": "2099-01-02 08:00:00",
            "state": "awake",
            "data_lag_min": 1,
            "signals": {"zero_steps_count": 20},
        }
    )

    late_rows = [
        {
            "poll_time": "2099-01-02 19:20:00",
            "state": "awake",
            "heart_rate": 109.0,
            "spo2": 80.9,
            "spo2_time": "10:48:00",
            "spo2_lag_min": 512,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:25:00",
            "state": "awake",
            "heart_rate": 113.5,
            "spo2": 80.9,
            "spo2_time": "10:48:00",
            "spo2_lag_min": 517,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
    ]

    events = []
    for row in late_rows:
        events.extend(engine.ingest_row(row))

    assert not any(e.type == "cardio_respiratory_strain" for e in events)


def test_acute_cardio_stress_113_only_emits_medium():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    for minute in range(30):
        engine.ingest_row(
            {
                "poll_time": f"2099-01-01 10:{minute:02d}:00",
                "state": "awake",
                "heart_rate": 88.0,
                "data_lag_min": 5,
                "signals": {"zero_steps_count": 20},
            }
        )

    rows = [
        {
            "poll_time": "2099-01-02 19:15:00",
            "state": "awake",
            "heart_rate": 109.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:20:00",
            "state": "awake",
            "heart_rate": 111.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:25:00",
            "state": "awake",
            "heart_rate": 113.5,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
    ]

    events = []
    for row in rows:
        events.extend(engine.ingest_row(row))

    acute_events = [e for e in events if e.type == "acute_cardio_stress"]
    assert len(acute_events) == 1
    assert acute_events[0].severity == "medium"


def test_acute_cardio_stress_120_for_20_min_emits_high():
    mod = _import_health_event_v2()
    engine = mod.HealthEventV2Engine()

    for minute in range(30):
        engine.ingest_row(
            {
                "poll_time": f"2099-01-01 10:{minute:02d}:00",
                "state": "awake",
                "heart_rate": 88.0,
                "data_lag_min": 5,
                "signals": {"zero_steps_count": 20},
            }
        )

    rows = [
        {
            "poll_time": "2099-01-02 19:10:00",
            "state": "awake",
            "heart_rate": 120.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:15:00",
            "state": "awake",
            "heart_rate": 121.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:20:00",
            "state": "awake",
            "heart_rate": 122.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
        {
            "poll_time": "2099-01-02 19:25:00",
            "state": "awake",
            "heart_rate": 123.0,
            "data_lag_min": 5,
            "signals": {"zero_steps_count": 20},
        },
    ]

    events = []
    for row in rows:
        events.extend(engine.ingest_row(row))

    acute_events = [e for e in events if e.type == "acute_cardio_stress"]
    assert len(acute_events) == 1
    assert acute_events[0].severity == "high"
