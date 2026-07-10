"""HTTP handler for VitalCast iOS app webhooks.

Runs inside a daemon thread (stdlib ``HTTPServer``, **no** async) and
shares ``VitalsStore`` / ``AlertsStore`` instances with the main MCP
asyncio loop via thread-safe locks.

Endpoints
---------
- ``POST /api/vitals`` — receive health metric samples
- ``POST /api/alert``  — receive a health alert event
- ``GET  /health``     — server health / uptime probe
"""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, ClassVar

from server.storage import VitalsStore, AlertsStore

logger = logging.getLogger(__name__)


class VitalCastHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for VitalCast push data.

    Class-level attributes are set before ``HTTPServer.serve_forever()``
    so that every request-handling instance has access to the shared
    stores and server metadata.
    """

    # Injected by the factory / module-level setup (see ``make_handler``)
    vitals_store: ClassVar[VitalsStore]
    alerts_store: ClassVar[AlertsStore]
    start_time: ClassVar[float]
    total_updates: ClassVar[list[int]]  # mutable list for atomic increment

    # BaseHTTPRequestHandler suppresses stderr logging by default
    # — re-enable via ``self.log_message`` override.
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("HTTP %s — %s", self.client_address[0], fmt % args)

    # ── routing ─────────────────────────────────────────────────

    def do_POST(self) -> None:
        path = self.path.rstrip("/")

        if path == "/api/vitals":
            self._handle_vitals()
        elif path == "/api/alert":
            self._handle_alert()
        else:
            self._send_json(404, {"status": "error", "message": f"Not found: {path}"})

    def do_GET(self) -> None:
        path = self.path.rstrip("/")

        if path == "/health":
            self._handle_health()
        else:
            self._send_json(404, {"status": "error", "message": f"Not found: {path}"})

    # ── POST /api/vitals ────────────────────────────────────────

    def _handle_vitals(self) -> None:
        body = self._read_body()
        if body is None:
            return  # _read_body already sent the 400 response

        if not isinstance(body, list):
            self._send_json(400, {"status": "error", "message": "Expected a JSON array"})
            return

        # Validate each entry has the required fields
        for i, entry in enumerate(body):
            if not isinstance(entry, dict):
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": f"Entry at index {i} is not a JSON object",
                    },
                )
                return
            missing = [k for k in ("type", "value", "unit", "date") if k not in entry]
            if missing:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": f"Entry at index {i} missing fields: {', '.join(missing)}",
                    },
                )
                return

        try:
            result = self.vitals_store.update(body)
            self.total_updates[0] += result["updated"]
            self._send_json(200, {"status": "ok", "updated": result["updated"]})
        except OSError as exc:
            logger.exception("Failed to persist vitals")
            self._send_json(500, {"status": "error", "message": str(exc)})

    # ── POST /api/alert ─────────────────────────────────────────

    def _handle_alert(self) -> None:
        body = self._read_body()
        if body is None:
            return

        if not isinstance(body, dict):
            self._send_json(400, {"status": "error", "message": "Expected a JSON object"})
            return

        try:
            alert_id = self.alerts_store.push(body)
            self._send_json(200, {"status": "alert_stored", "id": alert_id})
        except OSError as exc:
            logger.exception("Failed to persist alert")
            self._send_json(500, {"status": "error", "message": str(exc)})

    # ── GET /health ─────────────────────────────────────────────

    def _handle_health(self) -> None:
        uptime = time.time() - self.start_time
        # Peek at the last vitals timestamp (best-effort, not critical)
        all_vitals = self.vitals_store.get_all()
        last_vitals: str | None = None
        if all_vitals:
            last_vitals = max(
                (v.get("updated_at", "") or "" for v in all_vitals.values()),
                default=None,
            )

            total = self.total_updates[0]

        self._send_json(
            200,
            {
                "status": "ok",
                "uptime": uptime,
                "last_vitals": last_vitals,
                "total_updates": total,
            },
        )

    # ── helpers ─────────────────────────────────────────────────

    def _read_body(self) -> Any | None:
        """Read and parse the request body as JSON.

        Returns the parsed value on success, or ``None`` after sending
        a 400 response on failure.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length"})
            return None

        if length == 0:
            self._send_json(400, {"status": "error", "message": "Empty request body"})
            return None

        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"status": "error", "message": f"Invalid JSON: {exc}"})
            return None
        except OSError as exc:
            self._send_json(400, {"status": "error", "message": f"Read error: {exc}"})
            return None

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        """Send a JSON response with the given HTTP status code."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_handler(
    vitals_store: VitalsStore,
    alerts_store: AlertsStore,
) -> type[VitalCastHTTPHandler]:
    """Return a ``VitalCastHTTPHandler`` subclass wired to the given stores.

    Usage::

        handler_cls = make_handler(vitals_store, alerts_store)
        server = HTTPServer(("0.0.0.0", 8321), handler_cls)

    Note: we use ``_vs`` / ``_a_s`` / ``_tu`` aliases to work around a
    Python scoping quirk — class bodies treat names as local when they
    appear on the left-hand side, so ``vitals_store = vitals_store``
    would lose the closure reference.
    """
    # Use a mutable list so the handler can increment atomically
    _vs = vitals_store
    _a_s = alerts_store
    _tu: list[int] = [0]

    class _Handler(VitalCastHTTPHandler):
        vitals_store = _vs
        alerts_store = _a_s
        start_time = time.time()
        total_updates = _tu

    return _Handler



