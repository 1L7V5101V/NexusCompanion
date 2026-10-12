"""C9 persona 解析与 RelationshipState PG 写缝（c9-persona-relationship ADR-1/4/5，
c18-tenant-scoped-memory ADR-1/9/13 改目标表）。

- `resolve_persona_snapshot`：turn 入口在 tenant lane 内调用一次，取该 tenant 的
  PersonaProfile（PG 固定快照）与四格画像 + 关系状态当前值（画像专表），组成一对不可变
  快照随 ContextRequest 走 prompt 组装。无 profile（onboarding 未完成）返回 None →
  prompt 各 block 走单体回退语义。
- `PgRelationshipIO`：optimizer 的 RelationshipState 读写落 PG seam——写为
  `tenant_memory_profile_sections` 的 `relationship_state` 整行覆写 + `persona_audit_events`
  追加（同事务，ADR-5 单写者语义：串行由调用方 tenant lock / lane 保证，这里保证原子与审计）。
- `SqliteRelationshipIO`：同一件事的单机实现，语义与审计都保持一致（c18 ADR-10），差别只在
  事务靠 store 那条同步事务入口在同一个线程里连写两条。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.core.types import MemoryProfileSnapshot, PersonaSnapshot
from agent.memory_pg import PgMemoryProfileStore
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from core.memory.profile_store import SECTION_EXISTS_SQL
from infra.storage.tenancy import DEFAULT_TENANT

if TYPE_CHECKING:
    from agent.config_models import PersonaConfig


PERSONA_SNAPSHOT_SELECT_SQL = (
    "SELECT source, identity, personality_rules FROM tenant_persona_profiles "
    "WHERE tenant_id = :tenant_id"
)


def _snapshot_pair(
    profile: Any, assembly: Any, tenant_id: str
) -> tuple[PersonaSnapshot, MemoryProfileSnapshot]:
    """把 persona 行 + 画像装配结果拼成一对快照，两个后端共用这一份映射。

    分开写两份迟早分叉，而分叉的表现是同一轮对话在单机与多租户下注入不同内容。
    """
    return (
        PersonaSnapshot(
            tenant_id=tenant_id,
            source=str(profile[0]),
            identity=str(profile[1] or ""),
            personality_rules=str(profile[2] or ""),
            relationship_state=assembly.relationship_state,
        ),
        MemoryProfileSnapshot(
            tenant_id=tenant_id,
            long_term=assembly.long_term,
            pending=assembly.pending,
            recent_context=assembly.recent_context,
            history=assembly.history,
        ),
    )


async def resolve_persona_snapshot(
    session_factory: async_sessionmaker, tenant_id: str
) -> tuple[PersonaSnapshot, MemoryProfileSnapshot] | None:
    """解析 tenant 的 (persona 快照, 记忆画像快照)；无 profile 行返回 None。

    失败-open 到单体回退语义，不阻断 turn。

    两个快照出自**同一次会话借用**（ADR-1）：两次借用之间有时间缝，并发 optimizer 写入
    会让同一轮拿到两个瞬间的拼贴。画像那两条 SELECT 的正文与「行→字段」映射都在存储层，
    这里只借会话并把会话交出去（ADR-14 末段）。
    """
    async with session_factory() as sess:
        profile = (
            await sess.execute(
                text(PERSONA_SNAPSHOT_SELECT_SQL), {"tenant_id": tenant_id}
            )
        ).first()
        if profile is None:
            return None
        # `history_max_chars` 传 0（不截断）：`MemoryProfileSnapshot` 的 pending 与
        # history 今天没有任何组装期消费者（3.4 只接了 long_term 与 recent_context 两块），
        # 预算等出现消费者时按那一处定，现在不编数字。
        assembly = await PgMemoryProfileStore(
            session_factory, tenant_id
        ).read_assembly(sess, history_max_chars=0)
    return _snapshot_pair(profile, assembly, tenant_id)


class PgRelationshipIO:
    """optimizer 的 tenant RelationshipState 读写（ADR-9 写缝 + ADR-5 审计）。"""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    def _store(self, tenant_id: str) -> PgMemoryProfileStore:
        """按调用交来的 tenant_id 现造 store。

        store 是构造时绑租户的（6.4），本缝是每次调用绑租户的，这是本任务的一处形状错配。
        构造本身零 I/O，所以每次现造不花钱；4.1 的「租户 → 画像读写入口」解析器落地后，
        这一处收敛进解析器，改的是构造那一行而不是语义。
        """
        return PgMemoryProfileStore(self._sf, tenant_id)

    async def read(self, tenant_id: str) -> str:
        return await self._store(tenant_id).read_section("relationship_state")

    async def write(
        self, tenant_id: str, content: str, *, turn_id: str | None = None
    ) -> None:
        """单事务：`relationship_state` 整行覆写 + `relationship_update` 审计行。

        串行性由调用方保证（optimizer per-tenant lock / tenant lane）；本方法保证原子与
        审计。detail 只记字符数，不复制正文全文（ADR-5）。画像行的 upsert 走 store 并交出
        本会话，所以它不会自开事务、也不会把这笔事务提前提交。
        """
        store = self._store(tenant_id)
        async with self._sf() as sess, sess.begin():
            await store.write_section("relationship_state", content, session=sess)
            await sess.execute(
                text("""
                    INSERT INTO persona_audit_events
                        (tenant_id, actor, action, turn_id, detail)
                    VALUES (:tenant_id, 'optimizer', 'relationship_update', :turn_id,
                            CAST(:detail AS jsonb))
                    """),
                {
                    "tenant_id": tenant_id,
                    "turn_id": uuid.UUID(turn_id) if turn_id else None,
                    "detail": json.dumps({"chars": len(content)}),
                },
            )


class SqliteRelationshipIO:
    """单机后端的 RelationshipState 读写（c18 ADR-9/10：与 PG 同语义、同审计）。

    持有的不是连接而是 store：那个 store 自己开连接、自己有锁，而一个事务必须整段待在同一个
    线程里（原因见 `SqliteMemoryProfileStore.transaction` 的文档）。所以本类的 write 一次
    `asyncio.to_thread` 交进一个同步函数体，里面连着写画像行与审计行——两次写入同属一笔事务，
    与 PG 侧 `PgRelationshipIO.write` 的形状一致。

    归属是固定的：单机只有 `DEFAULT_TENANT`。传进别的租户不是「换个桶写」，是上游的租户归属
    没接上，所以直接抛而不是静默写到默认租户那一行上。
    """

    def __init__(self, profile_store: SqliteMemoryProfileStore) -> None:
        self._store = profile_store

    def _require_tenant(self, tenant_id: str) -> None:
        if tenant_id != self._store.tenant_id:
            raise ValueError(
                f"SQLite 画像存储按租户单例（当前 {self._store.tenant_id!r}），"
                f"收到 {tenant_id!r}：上游租户归属没接上"
            )

    async def read(self, tenant_id: str) -> str:
        self._require_tenant(tenant_id)
        return await self._store.read_section("relationship_state")

    async def write(
        self, tenant_id: str, content: str, *, turn_id: str | None = None
    ) -> None:
        self._require_tenant(tenant_id)
        await asyncio.to_thread(self._write_sync, content, turn_id)

    def _write_sync(self, content: str, turn_id: str | None) -> None:
        created_at = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._store.transaction() as db:
            self._store.write_section_sync(db, "relationship_state", content)
            db.execute(
                """
                INSERT INTO persona_audit_events
                    (id, tenant_id, actor, action, turn_id, detail, created_at)
                VALUES (:id, :tenant_id, 'optimizer', 'relationship_update', :turn_id,
                        :detail, :created_at)
                """,
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": self._store.tenant_id,
                    "turn_id": turn_id,
                    "detail": json.dumps({"chars": len(content)}),
                    "created_at": created_at,
                },
            )


class SqlitePersonaResolver:
    """单机后端的快照解析器，返回形状与 `resolve_persona_snapshot` 一致。

    所以 `bind_persona_resolver` 不必区分后端。与 PG 侧同一条要求：persona 行与画像两条
    SELECT 出自同一次借用（ADR-1），这里的一次借用就是 store 的一个同步事务。
    """

    def __init__(self, profile_store: SqliteMemoryProfileStore) -> None:
        self._store = profile_store

    async def __call__(
        self, tenant_id: str
    ) -> tuple[PersonaSnapshot, MemoryProfileSnapshot] | None:
        if tenant_id != self._store.tenant_id:
            raise ValueError(
                f"SQLite 画像存储按租户单例（当前 {self._store.tenant_id!r}），"
                f"收到 {tenant_id!r}：上游租户归属没接上"
            )
        return await asyncio.to_thread(self._resolve_sync, tenant_id)

    def _resolve_sync(
        self, tenant_id: str
    ) -> tuple[PersonaSnapshot, MemoryProfileSnapshot] | None:
        with self._store.transaction() as db:
            profile = db.execute(
                PERSONA_SNAPSHOT_SELECT_SQL, {"tenant_id": tenant_id}
            ).fetchone()
            if profile is None:
                return None
            assembly = self._store.read_assembly_sync(db, 0)
        return _snapshot_pair(profile, assembly, tenant_id)


def ensure_single_user_persona_rows(
    db_path: str | Path,
    persona: PersonaConfig,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """单机启动播种，两处都只在行不存在时写入（design ADR-10）。

    - `tenant_persona_profiles` 一行：`identity` / `personality_rules` / `self_model` 取
      `[agent.persona]`，`source` 取 `custom`（该列 CHECK 只允许 template/custom）。
    - 画像专表的 `relationship_state` 一行：内容同样取 `self_model`，对应文件后端里播种
      `memory/SELF.md` 那一步。

    播种之后 config 不再参与当前值：写缝演进的是那一行，重启不会把它盖回 config。用存在性
    探针而不是「读出来是空串」判断要不要播种——空串也可能是被合法清空过。

    两行在同一笔事务里，中途失败不留「有身份没关系状态」的半个租户。
    """
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    store = SqliteMemoryProfileStore(db_path, tenant_id)
    try:
        with store.transaction() as db:
            if (
                db.execute(
                    PERSONA_SNAPSHOT_SELECT_SQL, {"tenant_id": tenant_id}
                ).fetchone()
                is None
            ):
                db.execute(
                    """
                    INSERT INTO tenant_persona_profiles
                        (tenant_id, source, template_id, identity,
                         personality_rules, self_model, created_at)
                    VALUES (:tenant_id, 'custom', NULL, :identity,
                            :personality_rules, :self_model, :created_at)
                    """,
                    {
                        "tenant_id": tenant_id,
                        "identity": persona.identity,
                        "personality_rules": persona.personality_rules,
                        "self_model": persona.self_model,
                        "created_at": now,
                    },
                )
            seeded = (
                db.execute(
                    SECTION_EXISTS_SQL,
                    {"tenant_id": tenant_id, "category": "relationship_state"},
                ).fetchone()
                is None
            )
            if seeded:
                store.write_section_sync(
                    db, "relationship_state", persona.self_model
                )
    finally:
        store.close()


def build_persona_wiring(
    session_factory: async_sessionmaker,
) -> tuple[Any, PgRelationshipIO]:
    """PG durable 装配点：返回 (snapshot resolver, relationship io)。"""
    return (
        lambda tenant_id: resolve_persona_snapshot(session_factory, tenant_id),
        PgRelationshipIO(session_factory),
    )


__all__ = [
    "PgRelationshipIO",
    "SqlitePersonaResolver",
    "SqliteRelationshipIO",
    "build_persona_wiring",
    "ensure_single_user_persona_rows",
    "resolve_persona_snapshot",
]
