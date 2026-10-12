"""把共享 markdown 画像迁进租户专表（c18-tenant-scoped-memory task 7.1 / 7.2 / 7.3）。

用法：

    # 先看，不写
    python scripts/migrate_profile_to_tenant.py --workspace <dir> --tenant <id>
    # 确认对照无误后再执行
    python scripts/migrate_profile_to_tenant.py --workspace <dir> --tenant <id> --apply

`--tenant` 必填，缺参直接非零退出。目标后端按配置解析：PG 走该租户，SQLite 走固定租户
（单机运行时只读 `default`，迁进别的 id 不会有读者——脚本警告，不替你决定）。

五类语义不合并（ADR-8）：`MEMORY.md → long_term`、`RECENT_CONTEXT.md → recent_context`、
`SELF.md → relationship_state`、`HISTORY.md → history`、`PENDING.md → pending`、
`journal/YYYY-MM-DD.md → journal`（逐行，文件名日期占 `happened_at`，正文不拼日期前缀）。
整块类靠 `UNIQUE (tenant_id, category)` 覆写，日志类靠 `UNIQUE (tenant_id, dedup_key)` 判重，
键由正文摘要推出——同一来源重放两次，第二次零新增（spec「重放不产生重复条目」）。不走
`app_configs` 那套旧幂等键：它 128 字符单列主键、`tenant_id` 又不是主键，装不下
consolidation 的 `source_ref`（message id 的 JSON 数组），且键跨租户全局（ADR-13）。

原文件保持原样（7.3）：只读，不写、不改名、不删。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import load_config
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from core.memory.profile_store import MemoryProfileStoreApi
from infra.storage.tenancy import DEFAULT_TENANT

_JOURNAL_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")
_SECTION_FILES = {
    "long_term": "MEMORY.md",
    "recent_context": "RECENT_CONTEXT.md",
    "relationship_state": "SELF.md",
}
_LOG_FILES = {
    "history": "HISTORY.md",
    "pending": "PENDING.md",
}
_CONSOLIDATION_MARKER_RE = re.compile(r"^\s*<!--\s*consolidation.*?-->\s*$", re.MULTILINE)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dedup_key(category: str, text: str, *, date_str: str = "") -> str:
    prefix = f"{category}:{date_str}:" if date_str else f"{category}:"
    return f"{prefix}sha256:{_digest(text)}"


def _chunks(text: str) -> list[str]:
    """按空行分块——与 HISTORY.md 的追加格式（每条后跟空行）一致。"""
    return [chunk.strip() for chunk in re.split(r"\n\s*\n+", text.strip()) if chunk.strip()]


@dataclass(frozen=True)
class SourceFile:
    category: str
    path: Path
    text: str

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))

    def entries(self) -> list[tuple[str, datetime]]:
        """拆成要迁入的日志行；日期进 `happened_at`，正文保持原样。

        history / pending 的条目时间文件里没有独立字段，取文件 mtime——它不比真实事件时间
        准，但比「整批都写迁移当天」更接近原意，且重放稳定（mtime 不变）。journal 例外：
        日期就在文件名里，是准的。
        """
        if self.category.startswith("journal:"):
            date_str = self.category.split(":", 1)[1]
            stamp = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            return [(line.strip(), stamp) for line in self.text.splitlines() if line.strip()]
        stamp = datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
        return [(chunk, stamp) for chunk in _chunks(self.text)]


def read_source_files(workspace: Path) -> list[SourceFile]:
    """只读共享目录；文件不存在就是没有这一类，不创建。"""
    memory_dir = workspace / "memory"
    out: list[SourceFile] = []
    for category, name in {**_SECTION_FILES, **_LOG_FILES}.items():
        path = memory_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if category == "history":
            # HISTORY.md 里的 consolidation 标记行是文件后端的记账，不是画像内容。
            text = _CONSOLIDATION_MARKER_RE.sub("", text)
        stripped = text.strip()
        if stripped:
            out.append(SourceFile(category, path, stripped))
    journal_dir = memory_dir / "journal"
    if journal_dir.is_dir():
        for path in sorted(journal_dir.glob("*.md")):
            match = _JOURNAL_DATE_RE.search(path.name)
            if match is None:
                continue
            text = path.read_text(encoding="utf-8").strip()
            if text:
                out.append(SourceFile(f"journal:{match.group(1)}", path, text))
    return out


def _preview(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0][:60] if lines else ""


def _legacy_self_rows_sqlite(db_path: Path) -> tuple[int, int]:
    if not db_path.exists():
        return (0, 0)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT count(*), coalesce(sum(length(summary)), 0) FROM memory_items "
            "WHERE memory_type = 'self'"
        ).fetchone()
    except sqlite3.Error:
        # 老库里还没有 memory_items（表由主程序启动时建）——报告为 0，不编数字。
        return (0, 0)
    finally:
        conn.close()
    return (int(row[0]), int(row[1]))


async def _legacy_self_rows_pg(session_factory: Any, tenant_id: str) -> tuple[int, int]:
    """旧自我认知行的数量与字数——只报告，不删（7.3 的「不改写原样」同样管到旧行）。"""
    from sqlalchemy import text

    async with session_factory() as sess:
        got = (
            await sess.execute(
                text(
                    "SELECT count(*), coalesce(sum(length(summary)), 0) FROM memory_items "
                    "WHERE tenant_id = :t AND memory_type = 'self'"
                ),
                {"t": tenant_id},
            )
        ).one()
    return (int(got[0]), int(got[1]))


async def _build_store(
    backend: str, config: Any, workspace: Path, tenant_id: str
) -> tuple[MemoryProfileStoreApi, Callable[[], Awaitable[tuple[int, int]]], Callable[[], Any]]:
    """返回 (画像存储, 查旧 self 行的函数, 释放函数)。

    三个都从这里出，是因为「哪个后端」这个判断只该写一次：PG 要建引擎、SQLite 要校验库
    已存在，两边都各有自己的旧行查法与关闭方式。
    """
    if backend == "postgres":
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from agent.memory_pg import PgMemoryProfileStore
        from bootstrap.work_queue import async_pg_url

        url = async_pg_url(str(getattr(config.storage, "postgres_url", "") or ""))
        if not url:
            raise SystemExit("配置里 storage.postgres_url 为空，无法按 PG 后端迁移")
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return (
            PgMemoryProfileStore(factory, tenant_id),
            lambda: _legacy_self_rows_pg(factory, tenant_id),
            engine.dispose,
        )

    if tenant_id != DEFAULT_TENANT:
        print(
            f"警告：SQLite 运行时只读 {DEFAULT_TENANT!r}，迁进 {tenant_id!r} 的内容不会有读者。",
            file=sys.stderr,
        )
    db_path = workspace / "memory" / "memory2.db"
    if not db_path.exists():
        raise SystemExit(
            f"目标库不存在：{db_path}。SQLite 后端的画像表由主程序启动时建，"
            "请先用同一 workspace 启一次后端再迁移。"
        )
    store = SqliteMemoryProfileStore(db_path, tenant_id)

    async def _legacy() -> tuple[int, int]:
        return _legacy_self_rows_sqlite(db_path)

    return store, _legacy, store.close


async def _report(store: MemoryProfileStoreApi, sources: list[SourceFile]) -> None:
    for category, name in _SECTION_FILES.items():
        current = await store.read_section(category)
        src = next((s for s in sources if s.category == category), None)
        print(
            f"  {category:<19} 表内 {len(current):>7} 字"
            f" | {name:<19} {src.byte_size if src else 0:>7} 字节"
            f" | 首行 {_preview(current) or _preview(src.text if src else '')!r}"
        )
    for category in ("history", "pending", "journal"):
        current = await store.read_entries(category)
        sources_of = [
            s
            for s in sources
            if s.category == category or s.category.startswith(f"{category}:")
        ]
        want = sum(len(s.entries()) for s in sources_of)
        have = len(_chunks(current)) if current else 0
        print(
            f"  {category:<19} 表内 {have:>7} 行"
            f" | 来源 {want:>7} 行 | 现有正文 {len(current):>7} 字"
        )


async def _migrate(store: MemoryProfileStoreApi, sources: list[SourceFile]) -> dict[str, int]:
    counts = {"sections": 0, "new": 0, "skipped": 0}
    for source in sources:
        if source.category in _SECTION_FILES:
            await store.write_section(source.category, source.text)
            counts["sections"] += 1
            continue
        is_journal = source.category.startswith("journal:")
        category = "journal" if is_journal else source.category
        if category not in ("history", "pending", "journal"):
            continue
        date_str = source.category.split(":", 1)[1] if is_journal else ""
        for text, happened_at in source.entries():
            appended = await store.append_entry(
                category,
                text,
                happened_at=happened_at,
                dedup_key=_dedup_key(category, text, date_str=date_str),
            )
            counts["new" if appended else "skipped"] += 1
    return counts


async def _run(args: argparse.Namespace) -> int:
    workspace: Path = args.workspace
    config = load_config(args.config)
    backend = str(getattr(config.storage, "backend", "sqlite") or "sqlite")
    sources = read_source_files(workspace)
    if not sources:
        print(f"{workspace / 'memory'} 下没有可迁移的画像文件")
        return 0

    store, legacy_self_rows, closer = await _build_store(backend, config, workspace, args.tenant)
    try:
        rows, chars = await legacy_self_rows()
        print(f"后端：{backend}｜租户：{args.tenant}｜来源文件：{len(sources)} 份")
        if rows:
            print(
                f"  注意：memory_items 里还有 {rows} 行 memory_type='self'（{chars} 字）。"
                "那是迁移前的旧自我认知行，召回面仍会看到它；是否清理由人工判断，"
                "本脚本不自动删。"
            )
        await _report(store, sources)
        if not args.apply:
            print("dry-run：未写入任何东西，加 --apply 才执行。")
            return 0
        counts = await _migrate(store, sources)
        print(
            f"已写入：整块类 {counts['sections']} 格、日志新增 {counts['new']} 行、"
            f"判重跳过 {counts['skipped']} 行"
        )
        print("---- 写入后对照 ----")
        await _report(store, sources)
        return 0
    finally:
        result = closer()
        if asyncio.iscoroutine(result):
            await result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    # required=True 就是 fail-closed：缺参时 argparse 以 2 退出，库与共享目录都不碰。
    parser.add_argument("--tenant", help="画像归属的租户 id，必填")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只做 dry-run 对照")
    args = parser.parse_args(argv)

    if not args.tenant or not args.tenant.strip():
        parser.error("--tenant 必填：画像归属不能靠默认值猜")
    if not args.workspace.is_dir():
        print(f"workspace 不存在：{args.workspace}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
