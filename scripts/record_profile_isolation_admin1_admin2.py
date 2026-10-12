"""c18 task 8.3 的实机记录：admin1 / admin2 两个真租户的跨租户画像隔离。

用法（本地便携 PG 在跑，配置指向 `nexus` 库；租户由 8.1 的播种脚本建）：

    python scripts/record_profile_isolation_admin1_admin2.py --workspace <dir> \\
        --config config.local-chat.toml

检查的是泄漏面本体，不是单元替身：给租户 A 写满六类画像内容（三类整块 + 三类日志），
再用**运行时真正走的那条路**解析两个租户的快照（`resolve_persona_snapshot`，person 与
画像出自同一次借用）并交给 `ContextBuilder.render_sections`（ADR-15 那个唯一渲染器），
断言四件事：

1. A 的 canary 出现在 A 自己的系统提示里——没有这条，第 2 条就是空断言；
2. A 的 canary 完全不出现在 B 的系统提示里；
3. B 的画像整块与关系状态都是空的（空语义，不是「查不到就回退读共享目录」）；
4. A 的 canary 不在 `memory_items` 的任何分区里（画像不落召回面）。

写入的 canary 用完即清：`long_term` / `recent_context` 与三行日志删掉，
`relationship_state` 恢复成 8.1 播种的那份原文（这张表 A 上本来就有行，删掉就成了
形状漂移）。跑完数据库回到跑之前的形状。

不含 LLM 生成侧：这里断言的是「组装出来的提示里有什么」。生成那一头的实机演练要真钥匙
和真网络，归发版前的手工验收。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agent.config import load_config
from agent.core.types import ContextRequest
from agent.memory_pg import PgMemoryProfileStore
from bootstrap.persona import resolve_persona_snapshot

CANARY = "全金属外壳·admin1 专属话题（c18 8.3 实机记录）"
DEDUP_PREFIX = "c18-8.3-"
SECTION_CATEGORIES = ("long_term", "recent_context", "relationship_state")
ENTRY_CATEGORIES = ("pending", "history", "journal")


class _Skills:
    """`ContextBuilder` 只用得到的最小技能面替身：渲染画像不需要真技能库。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build_skills_summary(self) -> str:
        return "skill summary"

    def get_always_skills(self) -> list[str]:
        return []

    def load_skills_for_context(self, names: list[str]) -> str:
        return ""


def _asyncpg_url(url: str) -> str:
    # Windows 上 psycopg3 的 async 驱动拒绝 Proactor 事件循环，与 8.1 脚本同一处理。
    return url.replace("postgresql+psycopg://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


async def _tenant_by_label(sf: async_sessionmaker, label: str) -> str:
    async with sf() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT cc.tenant_id FROM test_accounts ta "
                    "JOIN canonical_conversations cc ON cc.account_id = ta.id "
                    "WHERE ta.display_name = :label ORDER BY cc.created_at"
                ),
                {"label": label},
            )
        ).all()
    if len(rows) != 1:
        raise SystemExit(f"label {label!r} 解析到 {len(rows)} 个租户，需要恰好 1 个")
    return str(rows[0][0])


async def _purge_leftovers(sf: async_sessionmaker, tenant_id: str) -> None:
    """清掉上一次中止留下的 canary，让本脚本可以被反复执行。"""
    async with sf() as sess, sess.begin():
        await sess.execute(
            text(
                "DELETE FROM tenant_memory_profile_entries "
                "WHERE tenant_id = :t AND dedup_key LIKE :k"
            ),
            {"t": tenant_id, "k": f"{DEDUP_PREFIX}%"},
        )
        await sess.execute(
            text(
                "DELETE FROM tenant_memory_profile_sections "
                "WHERE tenant_id = :t AND content LIKE :like"
            ),
            {"t": tenant_id, "like": f"%{CANARY}%"},
        )


async def _baseline_relationship(sf: async_sessionmaker, tenant_id: str) -> str:
    """收尾要复原的那份 `relationship_state`。

    行不存在或内容为空时退回 onboarding 种子（`tenant_persona_profiles.self_model`）：
    ADR-9 说当前值就是由它播下的，测试租户被上一次中止的 canary 盖掉时按种子复原，
    比留一个空格子更接近 8.1 建出来的形状。
    """
    store = PgMemoryProfileStore(sf, tenant_id)
    current = await store.read_section("relationship_state")
    if current:
        return current
    async with sf() as sess:
        row = (
            await sess.execute(
                text(
                    "SELECT self_model FROM tenant_persona_profiles WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        ).first()
    return str(row[0]) if row and row[0] else ""


async def _write_canaries(sf: async_sessionmaker, tenant_id: str) -> str:
    """给 A 写满六类画像内容，返回写前的 `relationship_state` 原文供收尾复原。"""
    await _purge_leftovers(sf, tenant_id)
    before = await _baseline_relationship(sf, tenant_id)
    store = PgMemoryProfileStore(sf, tenant_id)
    now = datetime.now(UTC)
    await store.write_section("long_term", f"## 用户事实\n- {CANARY}")
    await store.write_section(
        "recent_context", f"## Compression\n- 待延续话题：{CANARY}"
    )
    await store.write_section("relationship_state", f"## 关系状态\n- 聊到 {CANARY}")
    for category in ENTRY_CATEGORIES:
        await store.append_entry(
            category,
            f"{CANARY}（{category}）",
            happened_at=now,
            dedup_key=f"{DEDUP_PREFIX}{category}",
        )
    return before


async def _render(sf: async_sessionmaker, builder: Any, tenant_id: str) -> tuple[str, Any]:
    resolved = await resolve_persona_snapshot(sf, tenant_id)
    if resolved is None:
        raise SystemExit(f"租户 {tenant_id} 没有 persona 行：先跑 8.1 的播种脚本")
    persona, profile = resolved
    request = ContextRequest(
        history=[{"role": "user", "content": "上一轮"}],
        current_message="这次聊点什么",
        tenant_id=tenant_id,
        profile_snapshot=profile,
        persona_snapshot=persona,
        retrieved_memory_block="",
    )
    return builder.render_sections(request).system_prompt, persona


async def _restore(
    sf: async_sessionmaker, tenant_id: str, previous_relationship: str
) -> None:
    async with sf() as sess, sess.begin():
        for category in ("long_term", "recent_context"):
            await sess.execute(
                text(
                    "DELETE FROM tenant_memory_profile_sections "
                    "WHERE tenant_id = :t AND category = :c"
                ),
                {"t": tenant_id, "c": category},
            )
        await sess.execute(
            text(
                "DELETE FROM tenant_memory_profile_entries "
                "WHERE tenant_id = :t AND dedup_key LIKE :k"
            ),
            {"t": tenant_id, "k": f"{DEDUP_PREFIX}%"},
        )
        if previous_relationship:
            await sess.execute(
                text(
                    "INSERT INTO tenant_memory_profile_sections "
                    "(id, tenant_id, category, content, updated_at) "
                    "VALUES (gen_random_uuid(), :t, 'relationship_state', :c, now()) "
                    "ON CONFLICT (tenant_id, category) DO UPDATE SET content = :c"
                ),
                {"t": tenant_id, "c": previous_relationship},
            )
        else:
            await sess.execute(
                text(
                    "DELETE FROM tenant_memory_profile_sections "
                    "WHERE tenant_id = :t AND category = 'relationship_state'"
                ),
                {"t": tenant_id},
            )


def _show(heading: str, prompt: str) -> None:
    print(f"---- {heading}（系统提示 {len(prompt)} 字符）----")
    for block in prompt.split("\n\n"):
        if "Long-term Memory" in block or "Recent" in block or CANARY in block:
            print("  " + block.replace("\n", "\n  "))
    print()


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.storage.backend != "postgres":
        print("这份记录要多租户后端：storage.backend 需为 postgres。", file=sys.stderr)
        return 2
    engine = create_async_engine(_asyncpg_url(config.storage.postgres_url), poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant_a = await _tenant_by_label(sf, args.label_a)
        tenant_b = await _tenant_by_label(sf, args.label_b)
        print(f"label→tenant   {args.label_a}={tenant_a}   {args.label_b}={tenant_b}")

        written = await _write_canaries(sf, tenant_a)
        print(
            f"已给 {tenant_a} 写入 {len(SECTION_CATEGORIES)} 个整块 + "
            f"{len(ENTRY_CATEGORIES)} 行日志（canary：{CANARY}）"
        )

        import agent.context as context_module

        loader = context_module.SkillsLoader
        context_module.SkillsLoader = _Skills  # type: ignore[misc]
        try:
            builder = context_module.ContextBuilder(Path(args.workspace))
            prompt_a, persona_a = await _render(sf, builder, tenant_a)
            prompt_b, persona_b = await _render(sf, builder, tenant_b)
        finally:
            context_module.SkillsLoader = loader  # type: ignore[misc]

        _show(f"A 自己的系统提示：canary 应出现", prompt_a)
        _show(f"B 的系统提示：canary 应缺席", prompt_b)
        print(
            f"A 的关系状态字符={len(persona_a.relationship_state)}  "
            f"B 的关系状态字符={len(persona_b.relationship_state)}"
        )

        failures: list[str] = []
        if CANARY not in prompt_a:
            failures.append("canary 没进 A 自己的提示：B 侧的缺席就成了空断言")
        if CANARY in prompt_b:
            failures.append("canary 进了 B 的提示：画像层仍在跨租户泄漏")
        snapshot_b = await resolve_persona_snapshot(sf, tenant_b)
        profile_b = snapshot_b[1] if snapshot_b is not None else None
        assert profile_b is not None
        for field in ("long_term", "recent_context", "pending", "history"):
            value = str(getattr(profile_b, field) or "")
            print(f"  B 的 {field}：{len(value)} 字符")
            if CANARY in value:
                failures.append(f"B 的 {field} 里含 A 的内容")
        if CANARY in persona_b.relationship_state:
            failures.append("B 的关系状态里含 A 的内容")

        async with sf() as sess:
            recall_hits = (
                await sess.execute(
                    text("SELECT count(*) FROM memory_items WHERE summary LIKE :like"),
                    {"like": f"%{CANARY}%"},
                )
            ).scalar_one()
            entry_hits = (
                await sess.execute(
                    text(
                        "SELECT count(*) FROM tenant_memory_profile_entries "
                        "WHERE tenant_id <> :t AND content LIKE :like"
                    ),
                    {"t": tenant_a, "like": f"%{CANARY}%"},
                )
            ).scalar_one()
        print(f"召回面（memory_items 全部分区）含该 canary 的行数：{recall_hits}")
        print(f"其他租户的画像日志行含该 canary 的行数：{entry_hits}")
        if recall_hits:
            failures.append(f"画像内容出现在召回面 {recall_hits} 行")
        if entry_hits:
            failures.append(f"A 的 canary 出现在别的租户的画像日志里 {entry_hits} 行")

        await _restore(sf, tenant_a, written)
        async with sf() as sess:
            left_sections = (
                await sess.execute(
                    text(
                        "SELECT count(*) FROM tenant_memory_profile_sections "
                        "WHERE tenant_id = :t AND category <> 'relationship_state'"
                    ),
                    {"t": tenant_a},
                )
            ).scalar_one()
            left_entries = (
                await sess.execute(
                    text(
                        "SELECT count(*) FROM tenant_memory_profile_entries "
                        "WHERE dedup_key LIKE :k"
                    ),
                    {"k": f"{DEDUP_PREFIX}%"},
                )
            ).scalar_one()
            restored = (
                await sess.execute(
                    text(
                        "SELECT content FROM tenant_memory_profile_sections "
                        "WHERE tenant_id = :t AND category = 'relationship_state'"
                    ),
                    {"t": tenant_a},
                )
            ).scalar()
        print(
            f"清理后：A 的非关系状态整块 {left_sections} 行、canary 日志 {left_entries} 行、"
            f"relationship_state 复原={'是' if restored == written else '否'}"
        )
        if left_sections or left_entries:
            failures.append(f"canary 没清干净（整块 {left_sections}、日志 {left_entries}）")

        if failures:
            print("FAILED：")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("PASSED：画像段按租户隔离，B 看不见 A 的内容，且画像不落召回面。")
        return 0
    finally:
        engine.sync_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--label-a", default="admin1")
    parser.add_argument("--label-b", default="admin2")
    parsed = parser.parse_args(argv)
    return asyncio.run(_run(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
