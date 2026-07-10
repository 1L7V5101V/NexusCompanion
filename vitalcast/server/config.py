"""VitalCast configuration — environment variables and CLI arguments.

Resolution order (later wins):
  1. Default values in VitalCastConfig
  2. Environment variables (VITALCAST_HOST, VITALCAST_PORT, …)
  3. CLI arguments (--host, --port, …)
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VitalCastConfig:
    """Server configuration.

    All fields have sensible defaults that can be overridden via
    environment variables and/or CLI arguments.
    """

    host: str = "0.0.0.0"
    port: int = 8321
    data_dir: Path = Path.home() / ".vitalcast"
    max_alerts: int = 100

    @classmethod
    def from_env_and_args(cls) -> VitalCastConfig:
        """Build config by layering env vars over defaults, then CLI args over env.

        Note: ``slots=True`` means ``cls.port`` on a dataclass is a slot
        descriptor, not the default value — so we use explicit literals
        for the env-var fallbacks.
        """
        parser = cls._build_parser()
        args = parser.parse_args()

        host = os.environ.get("VITALCAST_HOST", "0.0.0.0")
        port = int(os.environ.get("VITALCAST_PORT", "8321"))
        data_dir = Path(os.environ.get("VITALCAST_DATA_DIR", str(Path.home() / ".vitalcast")))
        max_alerts = int(os.environ.get("VITALCAST_MAX_ALERTS", "100"))

        if args.host is not None:
            host = args.host
        if args.port is not None:
            port = args.port
        if args.data_dir is not None:
            data_dir = Path(args.data_dir)
        if args.max_alerts is not None:
            max_alerts = args.max_alerts

        return cls(host=host, port=port, data_dir=data_dir, max_alerts=max_alerts)

    @staticmethod
    def _build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="VitalCast MCP Health Data Bridge — "
            "receives Apple Watch vitals via HTTP and exposes them as MCP tools.",
        )
        parser.add_argument("--host", default=None, help="HTTP bind host (default: 0.0.0.0)")
        parser.add_argument("--port", type=int, default=None, help="HTTP bind port (default: 8321)")
        parser.add_argument("--data-dir", default=None, help="Data directory (default: ~/.vitalcast)")
        parser.add_argument(
            "--max-alerts",
            type=int,
            default=None,
            dest="max_alerts",
            help="Maximum queued alerts before oldest are dropped (default: 100)",
        )
        return parser
