"""查看调度器状态（运维脚本）。

按 `storage.backend` 分流（c11-explicit-schedules ADR-7）：

- `postgres` → 查 PG 的 `scheduled_jobs` / `schedule_executions`。durable 调度服务
  **不读** `schedules.json`，用 JSON 看会读到永不更新的旧派生物。
- 其它后端 → 读 `<workspace>/schedules.json`（legacy 单机路径）。

用法：

    python check_schedules.py                       # 默认 config.toml + 服务器 workspace
    python check_schedules.py --config config.server.toml
    python check_schedules.py --workspace /root/.nexus/workspace
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_WORKSPACE = Path("/root/.nexus/workspace")
_DEFAULT_DRIVER_PREFIXES = ("postgresql+psycopg://", "postgresql+psycopg2://")


def _dsn(url: str) -> str:
    """SQLAlchemy 的 psycopg 驱动前缀 → psycopg 原生 DSN。"""
    stripped = url.strip()
    for prefix in _DEFAULT_DRIVER_PREFIXES:
        if stripped.startswith(prefix):
            return "postgresql://" + stripped[len(prefix) :]
    return stripped


def show_postgres(config) -> int:
    import psycopg

    now = datetime.now(UTC)
    print(f"Current time (UTC): {now.isoformat()}")
    with psycopg.connect(_dsn(config.storage.postgres_url)) as conn:
        jobs = conn.execute(
            """
            SELECT name, tenant_id, trigger_kind, tier, timezone, status,
                   next_scheduled_for, last_outcome, revision,
                   delivery_channel, delivery_target
            FROM scheduled_jobs
            ORDER BY next_scheduled_for NULLS LAST, created_at DESC
            """
        ).fetchall()
        print(f"Number of jobs: {len(jobs)}")
        for (
            name,
            tenant_id,
            trigger,
            tier,
            tz,
            status,
            next_at,
            last_outcome,
            revision,
            channel,
            target,
        ) in jobs:
            when = next_at.isoformat() if next_at else "无待执行"
            state = ""
            if next_at is not None:
                aware = (
                    next_at
                    if next_at.tzinfo
                    else next_at.replace(tzinfo=UTC)
                )
                state = "   EXPIRED" if aware <= now else "   FUTURE"
            print(
                f"  {name or '(未命名)'}  [{status}/r{revision}]  {tier}/{trigger}  "
                f"tz={tz}  next={when}{state}  last={last_outcome or '-'}  "
                f"→ {channel}:{target}  tenant={tenant_id}"
            )

        outcomes = conn.execute(
            """
            SELECT status, skip_reason, count(*)
            FROM schedule_executions
            GROUP BY status, skip_reason
            ORDER BY count(*) DESC
            """
        ).fetchall()
        print(f"\nExecution outcomes ({len(outcomes)} buckets):")
        for status, skip_reason, total in outcomes:
            reason = f"  reason={skip_reason}" if skip_reason else ""
            print(f"  {status:<9} {total}{reason}")

        misses = conn.execute(
            """
            SELECT j.name, e.scheduled_for, e.status, e.skip_reason, e.error
            FROM schedule_executions e
            JOIN scheduled_jobs j ON j.id = e.job_id
            WHERE e.status IN ('missed', 'skipped', 'failed')
            ORDER BY e.scheduled_for DESC
            LIMIT 10
            """
        ).fetchall()
        print("\n最近 10 条 missed/skipped/failed（admin 面同名查询）:")
        if not misses:
            print("  （无）")
        for name, scheduled_for, status, skip_reason, error in misses:
            detail = skip_reason or error or "-"
            print(f"  {name or '(未命名)'}  {scheduled_for}  {status}  {detail}")
    return 0


def show_json(workspace: Path) -> int:
    path = workspace / "schedules.json"
    if not path.exists():
        print(f"未找到 {path}", file=sys.stderr)
        return 1
    jobs = json.loads(path.read_text())
    now = datetime.now(UTC)
    print(f"Current time (UTC): {now.isoformat()}")
    print(f"Number of jobs: {len(jobs)}  (legacy JSON 路径: {path})")
    for j in jobs:
        fire_at = datetime.fromisoformat(j["fire_at"])
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=UTC)
        expired = "EXPIRED" if fire_at < now else "FUTURE"
        print(
            f"  {j['name']}: fire_at={j['fire_at']}   {expired}   "
            f"run_count={j.get('run_count')}   enabled={j.get('enabled')}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml", help="TOML 配置文件路径")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="legacy JSON 路径的 workspace 目录",
    )
    args = parser.parse_args(argv)

    from agent.config_models import Config

    config = Config.load(args.config)
    if config.storage.backend == "postgres":
        return show_postgres(config)
    return show_json(args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())
