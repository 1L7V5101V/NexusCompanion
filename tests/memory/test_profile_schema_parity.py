"""c18-tenant-scoped-memory task 6.2：画像四表的跨后端结构一致性。

同一份结构有两个出处——PG 走 alembic（哪条迁移建了哪张表见下方 `_PG_MIGRATIONS`），
SQLite 走 `memory2/store.py` 的建表语句（design ADR-10 的主要代价）。以后只改一处就会让两种
后端静默分叉，而且分叉表现为「单机能用、PG 报错」或反之，不是启动即失败。本测试是
那处代价的缓解手段：把两份 DDL 解析成结构描述后逐表比对。

比的是四件事：**列名集合、NOT NULL 列集合、CHECK 白名单取值、唯一键覆盖的列组**。
类型不在比对范围内，因为 SQLite 这边没有 uuid / timestamptz 类型，`memory2/store.py`
的既有约定是把时间戳存 ISO 文本、id 存文本——那是本文件的既有风格，不是分叉。
类型漂移里真正要防的那一条（键列被加上长度上限）由
`test_dedup_key_has_no_length_cap_on_pg_side` 单独钉住：`app_configs.key` 是
`String(128)` 而 SQLite 不校验 VARCHAR 长度，同一个缺陷在两种后端只露出一半。

不依赖任何在线数据库：两份 DDL 都是本地源文件，所以这条测试在无 PG 的环境下也真实
生效，不会退化成 skip。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from memory2.store import SCHEMA as SQLITE_SCHEMA

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS_DIR = _REPO_ROOT / "alembic" / "versions"
# 画像两张表由 C18 建，persona 两张表是 C9 已有的——PG 侧按表去这两个文件里找建表语句。
# 局限（刻意的）：只认 CREATE TABLE，若以后有人用 ALTER TABLE 改这两张表的列，本测试
# 看不到那次改动，需要同步把它加进搜索范围。
_PG_MIGRATIONS = (
    "b7d3f5a9c1e4_c18_tenant_memory_profile.py",
    "e1f3a5c7b9d2_c9_persona_relationship.py",
)

PROFILE_TABLES = (
    "tenant_memory_profile_sections",
    "tenant_memory_profile_entries",
    "tenant_persona_profiles",
    "persona_audit_events",
)


class _Structure:
    """一张表的可比对结构。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, bool] = {}  # 列名 -> NOT NULL
        self.checks: dict[str, frozenset[str]] = {}  # 列名 -> 白名单取值
        self.unique_keys: set[tuple[str, ...]] = set()


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切分建表体，括号内的逗号不算分隔（CHECK 里全是括号）。"""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(ch)
    if "".join(current).strip():
        items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


def _find_body(text: str, table: str) -> str:
    """取 `CREATE TABLE <table> (...)` 的括号体。"""
    match = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\(",
        text,
        re.IGNORECASE,
    )
    if match is None:
        raise AssertionError(f"源文件里没有建表语句：{table}")
    start = match.end() - 1  # 左括号本身
    depth = 0
    for idx in range(start, len(text)):
        if text[idx] == "(":
            depth += 1
        elif text[idx] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : idx]
    raise AssertionError(f"{table} 的建表体括号不闭合")


def _check_target_column(clause: str) -> tuple[str, frozenset[str]] | None:
    """从 CHECK 子句里取 `col IN ('a','b',...)` 的列与取值集合。"""
    match = re.search(r"(\w+)\s+IN\s*\(([^)]*)\)", clause, re.IGNORECASE)
    if match is None:
        return None
    column = match.group(1).lower()
    values = frozenset(v.strip().strip("'\"") for v in match.group(2).split(",") if v.strip())
    return column, values


def _parse(text: str, table: str) -> _Structure:
    structure = _Structure(table)
    for item in _split_top_level(_find_body(text, table)):
        normalized = " ".join(item.split())
        check = re.match(r"(?:CONSTRAINT\s+\w+\s+)?CHECK\s*\(", normalized, re.IGNORECASE)
        if check:
            found = _check_target_column(normalized)
            assert found is not None, f"{table}: 有一条解析不出白名单的 CHECK -> {normalized}"
            structure.checks[found[0]] = found[1]
            continue
        unique = re.match(
            r"(?:CONSTRAINT\s+\w+\s+)?UNIQUE\s*\(([^)]*)\)", normalized, re.IGNORECASE
        )
        if unique:
            columns = tuple(c.strip().strip('"').lower() for c in unique.group(1).split(","))
            structure.unique_keys.add(columns)
            continue
        if re.match(r"(?:CONSTRAINT\s+\w+\s+)?PRIMARY\s+KEY\s*\(", normalized, re.IGNORECASE):
            columns = re.findall(r"\w+", normalized)
            columns = tuple(c.lower() for c in columns if c.lower() != "primary" and c.lower() != "key" and not c.lower().startswith("constraint"))
            structure.unique_keys.add(columns)
            continue
        if re.match(
            r"(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\s*\(", normalized, re.IGNORECASE
        ):
            # 不比对 FK，也不镜像进 SQLite：单机没有模板面，`persona_templates` 根本不
            # 建（ADR-10），PG 那条 `FOREIGN KEY (template_id) ... ON DELETE SET NULL`
            # 在 SQLite 侧没有可引用的表。FK 只在 PG 生效是有意的不对称。
            continue
        name_match = re.match(r"(\w+)\s+(\w+)", normalized)
        assert name_match is not None, f"{table}: 认不出这一列定义 -> {normalized}"
        column = name_match.group(1).lower()
        assert column not in {
            "constraint",
            "primary",
            "unique",
            "check",
            "foreign",
        }, f"{table}: 把表级约束当成列了 -> {normalized}"
        not_null = bool(re.search(r"\bNOT\s+NULL\b", normalized, re.IGNORECASE)) or bool(
            re.search(r"\bPRIMARY\s+KEY\b", normalized, re.IGNORECASE)
        )
        structure.columns[column] = not_null
        inline = re.search(r"\bCHECK\s*\((.+)\)\s*$", normalized, re.IGNORECASE)
        if inline:
            found = _check_target_column(inline.group(1))
            if found is not None:
                structure.checks[column] = found[1]
    for index in re.finditer(
        rf"CREATE\s+UNIQUE\s+INDEX\s+(?:IF NOT EXISTS\s+)?\w+\s+ON\s+{table}\s*\(([^)]*)\)",
        text,
        re.IGNORECASE,
    ):
        structure.unique_keys.add(
            tuple(c.strip().split()[0].strip('"').lower() for c in index.group(1).split(","))
        )
    return structure


def _parse_pg(table: str) -> _Structure:
    """PG 侧按表找建表语句所在迁移，再解析。"""
    for filename in _PG_MIGRATIONS:
        path = _VERSIONS_DIR / filename
        assert path.exists(), f"PG 迁移文件不在预期位置：{path}"
        text = path.read_text(encoding="utf-8")
        if re.search(rf"CREATE TABLE {table}\s*\(", text, re.IGNORECASE):
            return _parse(text, table)
    raise AssertionError(f"PG 侧迁移里找不到 {table} 的建表语句：{_PG_MIGRATIONS}")


def _parse_sqlite(table: str) -> _Structure:
    return _parse(SQLITE_SCHEMA, table)


@pytest.mark.parametrize("table", PROFILE_TABLES)
def test_column_name_sets_match(table: str) -> None:
    pg, sqlite = _parse_pg(table), _parse_sqlite(table)
    assert pg.columns == sqlite.columns, (
        f"{table} 列名/NOT NULL 分叉：\n  PG:     {pg.columns}\n  SQLite: {sqlite.columns}"
    )


@pytest.mark.parametrize("table", PROFILE_TABLES)
def test_check_whitelists_match(table: str) -> None:
    pg, sqlite = _parse_pg(table), _parse_sqlite(table)
    assert pg.checks == sqlite.checks, (
        f"{table} CHECK 白名单分叉：\n  PG:     {pg.checks}\n  SQLite: {sqlite.checks}"
    )


@pytest.mark.parametrize("table", PROFILE_TABLES)
def test_unique_keys_match(table: str) -> None:
    pg, sqlite = _parse_pg(table), _parse_sqlite(table)
    assert pg.unique_keys == sqlite.unique_keys, (
        f"{table} 唯一键覆盖的列组分叉：\n  PG:     {pg.unique_keys}\n  SQLite: {sqlite.unique_keys}"
    )


def test_both_backends_carry_tenant_id() -> None:
    """四张表两侧都必须有 tenant_id——这是「隔离断言在单机也能跑」的前提。"""
    for table in PROFILE_TABLES:
        assert "tenant_id" in _parse_pg(table).columns, table
        assert "tenant_id" in _parse_sqlite(table).columns, table


def test_profile_categories_are_disjoint_across_the_two_tables() -> None:
    """整块表与日志表的类别白名单不得重叠，否则「按行形状分两张」失去意义。"""
    sections = _parse_pg("tenant_memory_profile_sections").checks["category"]
    entries = _parse_pg("tenant_memory_profile_entries").checks["category"]
    assert not (sections & entries), f"两表类别白名单重叠：{sections & entries}"
    assert sections == frozenset({"long_term", "recent_context", "relationship_state"})
    assert entries == frozenset({"pending", "history", "journal"})


def _find_pg_body(table: str) -> str:
    for filename in _PG_MIGRATIONS:
        text = (_VERSIONS_DIR / filename).read_text(encoding="utf-8")
        if re.search(rf"CREATE TABLE {table}\s*\(", text, re.IGNORECASE):
            return _find_body(text, table)
    raise AssertionError(f"PG 侧迁移里找不到 {table} 的建表语句：{_PG_MIGRATIONS}")


def test_dedup_key_has_no_length_cap_on_pg_side() -> None:
    """防 app_configs.key String(128) 那个缺陷在新表复现。

    迁移里嵌消息 ID 数组的去重键可以远超 128 字符；SQLite 不校验 VARCHAR 长度，
    所以只比对「列名 + NOT NULL」抓不到这一类分叉，必须单独钉 PG 侧的类型。
    """
    definition = re.search(r"dedup_key\s+([\w()]+)", _find_pg_body("tenant_memory_profile_entries"), re.IGNORECASE)
    assert definition is not None, "PG 侧没有 dedup_key 这一列"
    assert definition.group(1).upper() == "TEXT", (
        f"dedup_key 必须是 TEXT（无长度上限），实际 {definition.group(1)}"
    )


def test_happened_at_is_not_null_on_pg_side() -> None:
    """ADR-8 的日期占列靠 NOT NULL 保证，不靠实现记得填——原缺口正是这么产生的。"""
    definition = re.search(
        r"happened_at\s+[\w()]+([^\n,]*)",
        _find_pg_body("tenant_memory_profile_entries"),
        re.IGNORECASE,
    )
    assert definition is not None, "PG 侧没有 happened_at 这一列"
    assert "NOT NULL" in definition.group(1).upper(), f"happened_at 必须 NOT NULL：{definition.group(1)}"
