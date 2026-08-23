"""负载结果数据结构与统计口径（C0 任务 2.3+）。

统计口径固定，供双后端一致（2.5）与可复现对账（2.6）共用：
- 成功率 = succeeded / attempted；attempted 为 0 时为 null。
- 延迟百分位按 nearest-rank：升序后取 max(0, ceil(p/100 * n) - 1) 位。
- 时间单位：输入（record）为秒，输出（summary）为毫秒。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "percentile",
    "LoadSpec",
    "TurnStats",
    "RunStats",
    "LoadResult",
]

_LATENCY_KEYS: tuple[str, ...] = ("p50", "p90", "p95", "p99", "max", "mean")


def percentile(values: list[float], pct: float) -> float:
    """nearest-rank 百分位；values 必须非空。"""
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered)) - 1
    return ordered[max(0, rank)]


@dataclass
class LoadSpec:
    """一次负载运行的输入参数；结果文件完整回写，用于对账与复现。"""

    scenario: str
    backend: str = "sqlite"
    tenants: tuple[str, ...] = ("default",)
    channels: tuple[str, ...] = ("cli",)
    rounds: int = 10
    concurrency: int = 4
    reasoner: str = "script"
    seed: int | None = None
    sim_latency_ms: float = 0.0
    sim_fail_rate: float = 0.0
    dry_run: bool = False
    workspace: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "backend": self.backend,
            "tenants": list(self.tenants),
            "channels": list(self.channels),
            "rounds": self.rounds,
            "concurrency": self.concurrency,
            "reasoner": self.reasoner,
            "seed": self.seed,
            "sim_latency_ms": self.sim_latency_ms,
            "sim_fail_rate": self.sim_fail_rate,
            "dry_run": self.dry_run,
            "workspace": self.workspace,
        }


@dataclass
class TurnStats:
    """单桶（整体或某 (tenant, channel)）的 turn 统计。"""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    latency_ms: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        if self.attempted == 0:
            return None
        return self.succeeded / self.attempted

    def record(self, *, ok: bool, latency_s: float) -> None:
        self.attempted += 1
        self.latency_ms.append(latency_s * 1000.0)
        if ok:
            self.succeeded += 1
        else:
            self.failed += 1

    def latency_summary(self) -> dict[str, float | None]:
        if not self.latency_ms:
            return {key: None for key in _LATENCY_KEYS}
        return {
            "p50": percentile(self.latency_ms, 50),
            "p90": percentile(self.latency_ms, 90),
            "p95": percentile(self.latency_ms, 95),
            "p99": percentile(self.latency_ms, 99),
            "max": max(self.latency_ms),
            "mean": sum(self.latency_ms) / len(self.latency_ms),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": self.success_rate,
            "latency_ms": self.latency_summary(),
        }


@dataclass
class RunStats:
    """一个后端的完整统计：整体 + 按 (tenant, channel) 分桶。"""

    backend: str
    overall: TurnStats = field(default_factory=TurnStats)
    by_bucket: dict[str, TurnStats] = field(default_factory=dict)

    def record(self, tenant: str, channel: str, *, ok: bool, latency_s: float) -> None:
        key = f"{tenant}:{channel}"
        bucket = self.by_bucket.setdefault(key, TurnStats())
        self.overall.record(ok=ok, latency_s=latency_s)
        bucket.record(ok=ok, latency_s=latency_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "overall": self.overall.to_dict(),
            "by_bucket": {
                key: stats.to_dict() for key, stats in sorted(self.by_bucket.items())
            },
        }


@dataclass
class LoadResult:
    """结果文件内容：输入参数 + 运行元数据 + 各后端统计。"""

    spec: LoadSpec
    runs: dict[str, RunStats]
    started_at: str
    finished_at: str
    duration_seconds: float
    git_revision: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "git_revision": self.git_revision,
            "notes": self.notes,
            "runs": {
                backend: stats.to_dict()
                for backend, stats in sorted(self.runs.items())
            },
        }
