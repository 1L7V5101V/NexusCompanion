"""Thread-safe JSON file storage for health vitals snapshot and alert queue.

Data is persisted to two JSON files:
  - *vitals.json* — overwritten on each POST /api/vitals (latest snapshot)
  - *alerts.json* — append-only queue consumed by get_alerts()

All public methods are thread-safe via ``threading.Lock``.  File writes
are crash-safe via atomic rename (write → .tmp → os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class VitalsStore:
    """Thread-safe store for the latest snapshot of each health metric.

    The file is overwritten on every ``update()`` call so the on-disk
    state always reflects the most recent data.  A ``threading.Lock``
    guards all reads and writes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ── public API ──────────────────────────────────────────────

    def update(self, samples: list[dict[str, Any]]) -> dict[str, int]:
        """Merge *samples* into the in-memory snapshot and flush to disk.

        Each sample dict **must** contain ``type``, ``value``, ``unit``,
        and ``date`` keys.  Entries without a valid string ``type`` are
        silently skipped (forward-compatible with unknown metric types).
        """
        now = _now_iso()
        updated = 0
        with self._lock:
            for sample in samples:
                sample_type = sample.get("type")
                if not isinstance(sample_type, str) or not sample_type:
                    continue
                self._data[sample_type] = {
                    "value": sample.get("value"),
                    "unit": sample.get("unit"),
                    "date": sample.get("date"),
                    "updated_at": now,
                }
                updated += 1
            self._atomic_write()
        return {"updated": updated}

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Return a copy of the entire vitals snapshot."""
        with self._lock:
            return dict(self._data)

    def get(self, metric_type: str) -> dict[str, Any] | None:
        """Return a single metric snapshot, or ``None`` if unknown."""
        with self._lock:
            entry = self._data.get(metric_type)
            if entry is None:
                return None
            return dict(entry)

    # ── internals ───────────────────────────────────────────────

    def _load(self) -> None:
        """Load the on-disk snapshot into memory (called once at startup)."""
        if not self._path.exists():
            self._data = {}
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load vitals snapshot from %s: %s", self._path, exc)
            self._data = {}

    def _atomic_write(self) -> None:
        """Crash-safe write: write to a temp file, then rename."""
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.error("Failed to write vitals snapshot: %s", exc)
            raise


class AlertsStore:
    """Thread-safe queue of pending health alerts backed by a JSON file.

    Alerts are appended via ``push()`` and atomically drained via
    ``pop_all()`` (returns all pending alerts and empties the queue).
    When the queue exceeds *max_alerts*, the oldest entries are dropped
    on the next ``push()``.
    """

    def __init__(self, path: Path, max_alerts: int = 100) -> None:
        self._path = path
        self._max = max_alerts
        self._lock = threading.Lock()
        self._counter = 0
        self._alerts: list[dict[str, Any]] = []
        self._load()

    # ── public API ──────────────────────────────────────────────

    def push(self, alert_body: dict[str, Any]) -> str:
        """Append one alert and persist the queue.

        Returns the generated alert ID (e.g. ``"alert_042"``).
        """
        with self._lock:
            self._counter += 1
            alert_id = f"alert_{self._counter:03d}"
            entry: dict[str, Any] = {
                "id": alert_id,
                "type": alert_body.get("type"),
                "value": alert_body.get("value"),
                "unit": alert_body.get("unit"),
                "timestamp": alert_body.get("timestamp"),
                "message": alert_body.get("message"),
            }
            self._alerts.append(entry)

            # Drop oldest if over limit
            if len(self._alerts) > self._max:
                self._alerts = self._alerts[-self._max :]

            self._atomic_write()
        return alert_id

    def pop_all(self) -> list[dict[str, Any]]:
        """Return all pending alerts and clear the queue (atomic drain).

        This is a destructive read — alerts are removed from storage
        so they are not returned again.
        """
        with self._lock:
            result = list(self._alerts)
            self._alerts.clear()
            self._atomic_write()
        return result

    # ── internals ───────────────────────────────────────────────

    def _load(self) -> None:
        """Load pending alerts from disk (called once at startup)."""
        if not self._path.exists():
            self._alerts = []
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data: list[dict[str, Any]] = json.load(f)
            self._alerts = data
            # Recover counter from the last known ID
            max_id = 0
            for entry in self._alerts:
                eid = entry.get("id", "")
                if eid.startswith("alert_"):
                    try:
                        num = int(eid.removeprefix("alert_"))
                        if num > max_id:
                            max_id = num
                    except ValueError:
                        logger.debug("无法解析 alert ID: %r", eid)
            self._counter = max_id
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load alerts from %s: %s", self._path, exc)
            self._alerts = []

    def _atomic_write(self) -> None:
        """Crash-safe write: write to a temp file, then rename."""
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._alerts, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.error("Failed to write alerts: %s", exc)
            raise


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
