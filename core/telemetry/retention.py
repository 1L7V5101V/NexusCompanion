"""观测数据三档 retention 契约与清理 job（C12，ADR-5）。

§10 PROPOSED DEFAULT「日志与审计保留」P-1 接受为初始值：operational 30 天、
audit 180 天；内容型 debug 数据取更短独立 TTL（初始 7 天，§5.9.17「更短」）。
全部可配置（dataclass 字段）；清理 sweep 按 mtime 过期删除文件型观测产物，
幂等（重跑零删除）、支持 dry-run 演练、目录缺失降级为空扫描、单文件失败
逐条记录不中断。PG 行级 retention 伴随 C2 durable control plane 落地
（本模块不触碰）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

__all__ = [
    "DEBUG_CONTENT_DEFAULT_DAYS",
    "OPERATIONAL_DEFAULT_DAYS",
    "AUDIT_DEFAULT_DAYS",
    "RetentionCategory",
    "RetentionPolicy",
    "SweepReport",
    "sweep_roots",
]

OPERATIONAL_DEFAULT_DAYS = 30
AUDIT_DEFAULT_DAYS = 180
DEBUG_CONTENT_DEFAULT_DAYS = 7

SweepRoot = Union[str, Path]
_SCONDS_PER_DAY = 86400.0


class RetentionCategory:
    """retention 类别：三档独立 TTL。"""

    OPERATIONAL = "operational"
    AUDIT = "audit"
    DEBUG_CONTENT = "debug_content"


@dataclass(frozen=True)
class RetentionPolicy:
    """三档 retention（天），全部可配置；Pilot 初始值 30/180/7。"""

    operational_days: int = OPERATIONAL_DEFAULT_DAYS
    audit_days: int = AUDIT_DEFAULT_DAYS
    debug_content_days: int = DEBUG_CONTENT_DEFAULT_DAYS

    def days_for(self, category: str) -> float:
        if category == RetentionCategory.OPERATIONAL:
            return float(self.operational_days)
        if category == RetentionCategory.AUDIT:
            return float(self.audit_days)
        if category == RetentionCategory.DEBUG_CONTENT:
            return float(self.debug_content_days)
        raise ValueError(f"未知 retention 类别: {category!r}")

    def ttl_seconds_for(self, category: str) -> float:
        return self.days_for(category) * _SCONDS_PER_DAY


@dataclass
class SweepReport:
    """一次 sweep 的结果报告（metadata，不含文件内容）。"""

    category: str
    root: Path
    scanned: int = 0
    deleted: int = 0
    kept: int = 0
    bytes_freed: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "root": str(self.root),
            "scanned": self.scanned,
            "deleted": self.deleted,
            "kept": self.kept,
            "bytes_freed": self.bytes_freed,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


def sweep_roots(
    roots: Mapping[str, Sequence[SweepRoot]],
    policy: RetentionPolicy,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> list[SweepReport]:
    """按类别根目录执行过期清理，返回逐根报告。

    幂等：同一批文件第二次执行删除数为 0。目录不存在视为空扫描（降级，
    不报错）。`dry_run=True` 只统计不删除，`deleted` 记录"将删除"数量。
    """
    current = time.time() if now is None else now
    reports: list[SweepReport] = []
    for category, category_roots in roots.items():
        ttl = policy.ttl_seconds_for(category)
        for root in category_roots:
            reports.append(
                _sweep_one(
                    category=category,
                    root=Path(root),
                    cutoff=current - ttl,
                    dry_run=dry_run,
                )
            )
    return reports


def _sweep_one(
    *,
    category: str,
    root: Path,
    cutoff: float,
    dry_run: bool,
) -> SweepReport:
    report = SweepReport(category=category, root=root, dry_run=dry_run)
    if not root.is_dir():
        return report
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        report.scanned += 1
        try:
            stat = path.stat()
        except OSError as exc:
            report.errors.append(f"stat failed: {path} ({exc})")
            continue
        if stat.st_mtime > cutoff:
            report.kept += 1
            continue
        if dry_run:
            report.deleted += 1
            report.bytes_freed += stat.st_size
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.errors.append(f"delete failed: {path} ({exc})")
            continue
        report.deleted += 1
        report.bytes_freed += stat.st_size
    return report


def prune_empty_dirs(root: SweepRoot) -> int:
    """清理 sweep 后遗留的空目录（自底向上），返回删除的目录数。

    根目录本身不删。供周期任务在 sweep 后收尾；失败逐目录忽略。
    """
    base = Path(root)
    if not base.is_dir():
        return 0
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        path = Path(dirpath)
        if path == base:
            continue
        try:
            path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed
