"""PersonaRepository / 快照解析 / RelationshipState IO 验收（tasks 1.2 / 3.1 / 3.3）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bootstrap.persona import PgRelationshipIO, resolve_persona_snapshot
from bootstrap.db.repository.persona_repo import (
    OnboardingAlreadyCompletedError,
    PersonaRepository,
)

pytestmark = pytest.mark.postgres


class _CountingSession(AsyncSession):
    """数「借了几次会话」用：`async_sessionmaker(class_=...)` 就是为换 Session 类准备的。"""

    created: ClassVar[int] = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).created += 1
        super().__init__(*args, **kwargs)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def test_onboarding_atomic_double_submit_rejected(
    persona_repo: PersonaRepository,
    make_tenant: Callable[..., Any],
) -> None:
    """onboarding 原子提交：并发双提交只成功一份；种子 + 审计同事务落库。"""
    tenant = await make_tenant(prefix="pt_atomic")
    tpl = await persona_repo.create_template(
        name="tpl-a", identity="身份A", personality_rules="规则A", self_model="自模型A"
    )

    profile = await persona_repo.submit_onboarding(
        tenant_id=tenant["tenant_id"],
        source="template",
        identity="身份A",
        personality_rules="规则A",
        self_model="自模型A",
        template_id=tpl["id"],
    )
    assert profile["source"] == "template"
    with pytest.raises(OnboardingAlreadyCompletedError):
        await persona_repo.submit_onboarding(
            tenant_id=tenant["tenant_id"],
            source="custom",
            identity="x",
            personality_rules="y",
            self_model="z",
        )
    # 提交后正文固定（第二次提交未覆盖）。
    again = await persona_repo.get_profile(tenant["tenant_id"])
    assert again["identity"] == "身份A" and again["personality_rules"] == "规则A"
    # 种子（RelationshipState 当前值）已建立，且在画像专表而不是召回表（c18 ADR-9/13）。
    resolved = await resolve_persona_snapshot(persona_repo._sf, tenant["tenant_id"])
    assert resolved is not None
    snap, _profile = resolved
    assert snap.relationship_state == "自模型A"
    async with persona_repo._sf() as sess:
        seeded = (
            await sess.execute(
                text(
                    "SELECT content FROM tenant_memory_profile_sections "
                    "WHERE tenant_id = :t AND category = 'relationship_state'"
                ),
                {"t": tenant["tenant_id"]},
            )
        ).scalar_one_or_none()
        self_rows = (
            await sess.execute(
                text(
                    "SELECT count(*) FROM memory_items "
                    "WHERE tenant_id = :t AND memory_type = 'self'"
                ),
                {"t": tenant["tenant_id"]},
            )
        ).scalar_one()
    assert seeded == "自模型A"
    assert int(self_rows) == 0, (
        "6.7 之后 onboarding 不该再往召回表写 self 行——那张表的类型过滤不是结构性的，"
        "一次普通召回就能把这格整段返回"
    )
    # 审计行同事务。
    audit = await persona_repo.list_audit(tenant["tenant_id"])
    assert [a["action"] for a in audit] == ["onboarding_submit"]


async def test_template_changes_do_not_touch_existing_profiles(
    persona_repo: PersonaRepository,
    make_tenant: Callable[..., Any],
) -> None:
    """spec「模板变更不覆盖已有 tenant」：停用/改模板后快照逐字不变。"""
    tenant = await make_tenant(prefix="pt_tpl")
    tpl = await persona_repo.create_template(
        name="tpl-b", identity="原始身份", personality_rules="原始规则", self_model="原始自模型"
    )
    await persona_repo.submit_onboarding(
        tenant_id=tenant["tenant_id"],
        source="template",
        identity=tpl["identity"],
        personality_rules=tpl["personality_rules"],
        self_model=tpl["self_model"],
        template_id=tpl["id"],
    )

    await persona_repo.set_template_enabled(tpl["id"], enabled=False)

    profile = await persona_repo.get_profile(tenant["tenant_id"])
    assert profile["identity"] == "原始身份"
    enabled = await persona_repo.list_templates(enabled_only=True)
    assert all(t["name"] != "tpl-b" for t in enabled)  # 只影响后续 onboarding


async def test_profiles_are_tenant_isolated(
    persona_repo: PersonaRepository,
    make_tenant: Callable[..., Any],
) -> None:
    """spec「跨 tenant 不串」：A/B 两 tenant 快照互不可见、互不覆盖。"""
    ta, tb = await make_tenant(prefix="pt_iso_a"), await make_tenant(prefix="pt_iso_b")
    await persona_repo.submit_onboarding(
        tenant_id=ta["tenant_id"], source="custom",
        identity="A的身份", personality_rules="A的规则", self_model="A的自模型",
    )
    await persona_repo.submit_onboarding(
        tenant_id=tb["tenant_id"], source="custom",
        identity="B的身份", personality_rules="B的规则", self_model="B的自模型",
    )
    resolved_a = await resolve_persona_snapshot(persona_repo._sf, ta["tenant_id"])
    resolved_b = await resolve_persona_snapshot(persona_repo._sf, tb["tenant_id"])
    assert resolved_a is not None and resolved_b is not None
    snap_a, _profile_a = resolved_a
    snap_b, _profile_b = resolved_b
    assert snap_a.identity == "A的身份" and snap_a.relationship_state == "A的自模型"
    assert snap_b.identity == "B的身份" and snap_b.relationship_state == "B的自模型"


async def test_snapshot_none_before_onboarding(
    persona_repo: PersonaRepository,
    make_tenant: Callable[..., Any],
) -> None:
    """onboarding 未完成 → 快照 None（prompt 走单体回退语义）。"""
    tenant = await make_tenant(prefix="pt_none")
    assert await resolve_persona_snapshot(persona_repo._sf, tenant["tenant_id"]) is None


async def test_relationship_io_write_updates_and_audits(
    persona_repo: PersonaRepository,
    make_tenant: Callable[..., Any],
) -> None:
    """ADR-5 单写者：io.write 原地覆盖当前值 + 追加审计；读回逐字一致。"""
    tenant = await make_tenant(prefix="pt_io")
    await persona_repo.submit_onboarding(
        tenant_id=tenant["tenant_id"], source="custom",
        identity="身份", personality_rules="规则", self_model="初始关系状态",
    )
    io_ = PgRelationshipIO(persona_repo._sf)

    assert await io_.read(tenant["tenant_id"]) == "初始关系状态"
    await io_.write(tenant["tenant_id"], "更新后的关系状态 v1")
    await io_.write(tenant["tenant_id"], "更新后的关系状态 v2")
    assert await io_.read(tenant["tenant_id"]) == "更新后的关系状态 v2"

    audit = await persona_repo.list_audit(tenant["tenant_id"])
    updates = [a for a in audit if a["action"] == "relationship_update"]
    assert len(updates) == 2
    for a in updates:
        detail = a["detail"]
        if isinstance(detail, str):  # jsonb 经驱动可能回字符串
            detail = json.loads(detail)
        assert "chars" in detail


async def test_no_revision_tables(
    persona_pg_url: str,
    exec_sql: Callable[..., Any],
) -> None:
    """task 5.4 / spec「无版本表」：persona 域不存在 revision/version/history 表。"""
    rows = exec_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'"
    )
    names = {
        r[0]
        for r in rows
        if r[0].startswith(("persona", "tenant_persona"))
    }
    assert {"persona_templates", "tenant_persona_profiles", "persona_audit_events"} <= names
    assert not any(
        "revision" in n or "version" in n or "history" in n for n in names
    ), names


@pytest.fixture()
def counting_factory(persona_pg_url: str) -> Iterator[async_sessionmaker]:
    engine = create_async_engine(
        persona_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, class_=_CountingSession, expire_on_commit=False)
    engine.sync_engine.dispose()


async def test_prefetch_borrows_one_session(
    persona_repo: PersonaRepository,
    counting_factory: async_sessionmaker,
    make_tenant: Callable[..., Any],
) -> None:
    """c18 task 3.2 / ADR-1：画像、关系状态与身份出自同一次借用，一轮只借一次。"""
    from agent.memory_pg import PgMemoryProfileStore

    tenant = await make_tenant(prefix="pt_pre")
    await persona_repo.submit_onboarding(
        tenant_id=tenant["tenant_id"], source="custom",
        identity="身份", personality_rules="规则", self_model="关系当前值",
    )
    store = PgMemoryProfileStore(persona_repo._sf, tenant["tenant_id"])
    await store.write_section("long_term", "长期画像")
    await store.append_entry(
        "pending", "待合并", happened_at=datetime.now(timezone.utc)
    )

    _CountingSession.created = 0
    resolved = await resolve_persona_snapshot(counting_factory, tenant["tenant_id"])
    assert resolved is not None
    snapshot, profile = resolved

    assert _CountingSession.created == 1, (
        f"一轮预取借了 {_CountingSession.created} 次会话——两次借用之间有时间缝，"
        "并发 optimizer 写入会让同一轮拿到两个瞬间的拼贴"
    )
    assert snapshot.relationship_state == "关系当前值"
    assert profile.long_term == "长期画像"
    assert profile.pending == "待合并"
    assert profile.history == ""
