"""建/复用两个常驻本地测试租户，并打印 label → tenant_id 映射（c18 task 8.1）。

用法：

    # 建号（幂等：label 已存在就复用，不重复建）
    python scripts/seed_local_test_tenants.py --workspace <dir> --config config.toml
    # 只读解析：重启后端后证明按 display_name 仍能解析出同一对 tenant_id
    python scripts/seed_local_test_tenants.py --workspace <dir> --config config.toml --resolve

ADR-7：不改租户 id 生成规则。`admin1` / `admin2` 只写进 `test_accounts.display_name`，
`tenant_id` 仍是 `pilot-<12 位随机>`，所以看日志要先查这张映射表——本脚本打印的就是它。

开户走的是与 `POST /api/admin/test-accounts` 同一条 seam（`create_account` +
`run_pending`），并把分区 DDL 与 memory engine 初始绑定接成同一次执行的副作用，因此
本地建出来的租户形状应当与生产一致；不一致就是缺陷，会直接体现在这份输出里。

persona 行是 8.1 的验收要求（`tenant_persona_profiles` 必须有行）。这里用 `agent.persona`
的进程级常量作 identity/personality_rules，只为满足「有快照可解析」；**那不是该租户应有的
身份内容**，身份画像的租户化归另开的 change。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agent.config import load_config
from agent.persona import NEXUS_IDENTITY, PERSONALITY_RULES
from bootstrap.auth.runtime import AuthRuntime, create_auth_runtime
from bootstrap.db.repository.persona_repo import (
    OnboardingAlreadyCompletedError,
    PersonaRepository,
)
from bootstrap.memory_binding import TenantMemoryEngineBindingService
from infra.storage.factory import create_storage_runtime
from infra.storage.provisioning import TenantProvisioningService

DEFAULT_LABELS = ("admin1", "admin2")


def _labels(raw: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in raw.split(",") if item.strip())
    return items or DEFAULT_LABELS


async def _ensure_persona_row(
    persona: PersonaRepository, tenant_id: str, label: str
) -> str:
    try:
        await persona.submit_onboarding(
            tenant_id=tenant_id,
            source="custom",
            identity=NEXUS_IDENTITY,
            personality_rules=PERSONALITY_RULES,
            self_model=f"## 用户事实\n- 本地测试租户 {label}（c18 task 8.1 播种）",
            # `persona_audit_events.actor` 有 CHECK：只允许 user/optimizer/admin。
            actor="admin",
        )
        return "created"
    except OnboardingAlreadyCompletedError:
        return "already"


async def _resolve(
    runtime: AuthRuntime, labels: tuple[str, ...]
) -> list[dict[str, str]]:
    """按 display_name 解析 label → tenant_id，不写任何东西。"""
    accounts = await runtime.provisioning.list_accounts()
    rows: list[dict[str, str]] = []
    for label in labels:
        matched = [acc for acc in accounts if acc.get("display_name") == label]
        if not matched:
            rows.append({"label": label, "error": "account not found"})
            continue
        if len(matched) > 1:
            rows.append({"label": label, "error": "label maps to several accounts"})
            continue
        account = matched[0]
        conversations = await runtime.canonical_repo.list_conversations_by_account(
            account["id"]
        )
        rows.append(
            {
                "label": label,
                "account_id": str(account["id"]),
                "status": str(account["status"]),
                "tenant_id": ",".join(str(item["tenant_id"]) for item in conversations)
                or "(none)",
            }
        )
    return rows


async def _seed(
    runtime: AuthRuntime, persona: PersonaRepository, labels: tuple[str, ...]
) -> list[dict[str, str]]:
    for label in labels:
        existing = await _resolve(runtime, (label,))
        if "error" not in existing[0]:
            continue
        created = await runtime.provisioning.create_account(display_name=label)
        print(f"建号 {label} → account {created['account']['id']}")
        processed = await runtime.provisioning.run_pending(max_jobs=32)
        print(f"provisioning 执行 {processed} 个 job")
    rows = await _resolve(runtime, labels)
    for row in rows:
        if "error" in row or row.get("status") != "active":
            continue
        states = []
        for tenant_id in row["tenant_id"].split(","):
            states.append(f"{tenant_id}:{await _ensure_persona_row(persona, tenant_id, row['label'])}")
        row["persona"] = ",".join(states)
    return rows


def _print(rows: list[dict[str, str]]) -> None:
    for row in rows:
        if "error" in row:
            print(f"label={row['label']}\t{row['error']}")
            continue
        print(
            "\t".join(
                f"{key}={row[key]}"
                for key in ("label", "account_id", "status", "tenant_id")
            )
            + f"\tpersona={row.get('persona', 'not-checked')}"
        )


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.storage.backend != "postgres":
        print(
            f"本地验收台要的是多租户形状：storage.backend 需为 postgres，"
            f"当前是 {config.storage.backend!r}。",
            file=sys.stderr,
        )
        return 2
    # Windows 上 psycopg3 的 async 驱动拒绝 Proactor 事件循环，脚本固定换 asyncpg。
    config.storage.postgres_url = (
        config.storage.postgres_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
        .replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    args.workspace.mkdir(parents=True, exist_ok=True)

    storage_runtime = create_storage_runtime(
        config.storage,
        args.workspace / "memory" / "memory2.db",
        args.workspace / "sessions.db",
    )
    engine = create_async_engine(config.storage.postgres_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = None
    try:
        backend = storage_runtime.memory_backend
        if backend is None:
            print("PG backend 未装配", file=sys.stderr)
            return 2
        provisioning = TenantProvisioningService(backend, run_db=storage_runtime.run_db)
        bindings = TenantMemoryEngineBindingService(session_factory, ready_engines=("default",))

        async def _partition_step(tenant_id: str) -> None:
            # `request_provisioning` 只入队（worker 在常驻后端里跑），脚本进程内直接把
            # 幂等 DDL 落地，否则建出来的租户没有分区。
            _ = await provisioning.request_provisioning(tenant_id)
            await storage_runtime.run_db(provisioning.provision_tenant, tenant_id)

        async def _binding_ensure(tenant_id: str) -> None:
            await bindings.ensure_binding(tenant_id)

        runtime = create_auth_runtime(
            config=config,
            workspace=args.workspace,
            partition_step=_partition_step,
            binding_ensure=_binding_ensure,
        )
        persona = PersonaRepository(session_factory)
        labels = _labels(args.labels)
        rows = (
            await _resolve(runtime, labels)
            if args.resolve
            else await _seed(runtime, persona, labels)
        )
        _print(rows)
        if args.resolve and any("error" in row for row in rows):
            return 1
        return 0
    finally:
        if runtime is not None:
            await runtime.aclose()
        engine.sync_engine.dispose()
        storage_runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--labels", default=",".join(DEFAULT_LABELS), help="逗号分隔的 display_name"
    )
    parser.add_argument(
        "--resolve", action="store_true", help="只解析映射，不建号、不写 persona 行"
    )
    parsed = parser.parse_args(argv)
    return asyncio.run(_run(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
