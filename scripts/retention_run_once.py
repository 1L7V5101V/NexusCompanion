"""retention 单轮执行入口（p0-retention-wiring task 8.6 上线前演练 / 实删）。

用法（部署环境，进程外运行，不影响在线服务）：

    # 演练（默认，零变更，只出"将删除"报告）——实删前必须先跑一轮并存证：
    python scripts/retention_run_once.py --config config.toml

    # 实删单轮（确认演练报告符合预期后再放开）：
    python scripts/retention_run_once.py --config config.toml --live

    # 一键回退：config.toml [agent.retention] enabled = false
    # （周期任务不装配；本脚本仍可显式演练，但不会自动删除任何数据）

报告为 JSON（stdout），同时打印逐实体摘要；实删轮的删除数应与前一演练轮一致。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import load_config
from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.retention import RetentionSweeper
from bootstrap.work_queue import async_pg_url


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--live",
        action="store_true",
        help="实删单轮（默认 dry-run 演练，零变更）",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if config.storage.backend != "postgres":
        print(
            json.dumps(
                {"error": "storage.backend != postgres；retention 只接线 PG durable 路径"}
            )
        )
        return 2
    engine = create_engine(
        DatabaseConfig(
            url=async_pg_url(config.storage.postgres_url),
            pool_size=config.storage.pool_size,
        )
    )
    session_factory = create_session_factory(engine)
    try:
        sweeper = RetentionSweeper(session_factory, config.retention)
        report = await sweeper.run_once(dry_run=not args.live)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
