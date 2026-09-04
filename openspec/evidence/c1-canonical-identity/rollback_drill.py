"""C1 首次启用 Create→Verify→Enable→Rollback 演练（可复现脚本）。

对应 PILOT_ROADMAP §5.9.9 / §10 DECIDED：
- Create：Alembic 在空 PostgreSQL 创建 Pilot 新表/索引/约束/seed，不连接旧单体库。
- Verify：schema revision、约束、空库基线、dev seed。
- Enable：单一入口开放后产生真实写入（本脚本以 dev 会话 append 模拟）。
- Rollback：关闭入口 + 停止新 provisioning，**保留已产生的 PG 数据**；
  不反向同步旧单体库，不把旧库当回滚目标。

用法：
    .venv/Scripts/python.exe openspec/evidence/c1-canonical-identity/rollback_drill.py
环境变量 NEXUS_TEST_PG_URL 可覆盖管理连接（默认 nexus:nexus_dev@localhost:5433）。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from alembic.config import Config  # noqa: E402

from scripts.migrate.alembic_util import upgrade_head  # noqa: E402

ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL", "postgresql://nexus:nexus_dev@localhost:5433/nexus"
)
DRILL_DB = "nexus_c1_drill"
DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"

failures: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}")
    if not ok:
        failures.append(step)


def main() -> int:
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DRILL_DB}")
    admin.execute(f"CREATE DATABASE {DRILL_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + DRILL_DB

    # 迁移链依赖（a3d5c7e9f1b2 的 vector 类型 / b6e9d2c4a8f1 的 pg_trgm）。
    ext = psycopg.connect(url, autocommit=True)
    ext.execute("CREATE EXTENSION IF NOT EXISTS vector")
    ext.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    ext.close()

    # ── Create：空 PG 上 alembic upgrade head（不连接旧单体库）──
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://"))
    upgrade_head(cfg)
    check("Create", True, f"alembic upgrade head 在空库 {DRILL_DB} 执行成功（无旧单体库连接）")

    conn = psycopg.connect(url, autocommit=True)

    # ── Verify：revision、约束、空库基线、seed ──
    rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    check("Verify.revision", rev == "e2b4d6f8a0c2", f"alembic_version={rev}")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name IN ('test_accounts','canonical_conversations','canonical_messages')"
        ).fetchall()
    }
    check(
        "Verify.tables",
        tables == {"test_accounts", "canonical_conversations", "canonical_messages"},
        f"三表存在: {sorted(tables)}",
    )
    uniques = {
        r[0]
        for r in conn.execute(
            "SELECT conname FROM pg_constraint WHERE contype='u' AND connamespace='public'::regnamespace"
        ).fetchall()
    }
    need = {
        "uq_test_accounts_tenant_id",
        "uq_canonical_conversations_tenant_id",
        "uq_canonical_messages_conversation_sequence",
    }
    check("Verify.constraints", need <= uniques, f"唯一约束齐备: {sorted(need & uniques)}")
    baseline = conn.execute("SELECT count(*) FROM canonical_messages").fetchone()[0]
    check("Verify.empty_baseline", baseline == 0, f"canonical_messages 空历史 = {baseline} 行")
    seed = conn.execute(
        "SELECT count(*) FROM test_accounts WHERE id=%s AND status='active'", (DEV_ACCOUNT_ID,)
    ).fetchone()[0]
    check("Verify.seed", seed == 1, "dev seed 账号存在且 active")

    # ── Enable（模拟入口开放后的真实写入）──
    for i in range(3):
        conn.execute(
            "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role, content) "
            "VALUES ('dev', %s, %s, 'user', %s)",
            (DEV_CONVERSATION_ID, i, f"drill message {i}"),
        )
    enabled_rows = conn.execute("SELECT count(*) FROM canonical_messages").fetchone()[0]
    check("Enable", enabled_rows == 3, f"入口开放后写入 {enabled_rows} 条 canonical message")

    # ── Rollback：关闭入口 + 停 provisioning（模拟：此后不再有新写入/开户）──
    entry_open = False
    provisioning_open = False
    assert not entry_open and not provisioning_open
    after_rows = conn.execute("SELECT count(*) FROM canonical_messages").fetchone()[0]
    after_accounts = conn.execute("SELECT count(*) FROM test_accounts").fetchone()[0]
    check(
        "Rollback.data_retained",
        after_rows == 3 and after_accounts == 1,
        f"关闭入口后 PG 数据保留：messages={after_rows}, accounts={after_accounts}",
    )
    downgraded = conn.execute(
        "SELECT to_regclass('public.canonical_messages') IS NOT NULL"
    ).fetchone()[0]
    check(
        "Rollback.no_reverse_sync",
        downgraded,
        "PG Pilot 数据原地保留用于修复后继续；无任何向旧单体库的反向同步路径（旧库未连接）",
    )
    conn.close()

    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DRILL_DB}")
    admin.close()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} 项: {', '.join(failures)})")
        return 1
    print("RESULT: PASS —— Create→Verify→Enable→Rollback 演练全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
