"""C1 静态契约测试（无 PG 依赖，CI 可跑）。

1. canonical identity 契约 fixture 结构与冻结语义自检；
2. canonical 实现模块的 SQLite 隔离扫描：不 import/连接 SQLite、不引用
   DEFAULT_TENANT（§10 DECIDED：不 fallback、不双写、不反向同步）。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "canonical_identity_chain.json"

CANONICAL_MODULES = [
    REPO_ROOT / "bootstrap" / "db" / "models" / "canonical.py",
    REPO_ROOT / "bootstrap" / "db" / "repository" / "canonical_repo.py",
    REPO_ROOT / "bootstrap" / "identity.py",
    REPO_ROOT / "alembic" / "versions" / "e2b4d6f8a0c2_c1_canonical_identity.py",
    REPO_ROOT / "alembic" / "versions" / "c4d8f2a6e9b3_c1_account_n_tenants.py",
]

FORBIDDEN_TOKENS = ("sqlite", "default_tenant")


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_structure_and_frozen_semantics() -> None:
    fixture = _load_fixture()
    assert fixture["version"] == 2
    assert fixture["derivation_chain"].startswith("trusted principal")

    assert fixture["positive_cases"], "必须有正向用例"
    for case in fixture["positive_cases"]:
        assert case["input"]["kind"] in {"tenant", "account_id"}, case["name"]
        if case["input"]["kind"] == "tenant":
            # tenant 级解析恒为单一归属三元组。
            assert set(case["expected"]) == {
                "account_id",
                "tenant_id",
                "conversation_id",
            }
        else:
            # 账号级解析为「账号 + 其 agent（tenant）列表」。
            assert set(case["expected"]) == {"account_id", "agents"}
            assert case["expected"]["account_id"] == case["input"]["value"]
            assert case["expected"]["agents"], case["name"]
            for agent in case["expected"]["agents"]:
                assert set(agent) == {"tenant_id", "conversation_id"}, case["name"]

    assert fixture["negative_cases"], "必须有负向用例"
    for case in fixture["negative_cases"]:
        assert case["expected_error"] == "identity_resolution_error", case["name"]

    # 默认租户显式列为负向用例（无 binding 拒绝，§5.9.2）。
    negative_values = {case["input"]["value"] for case in fixture["negative_cases"]}
    assert "default" in negative_values

    stream = fixture["message_stream"]
    assert stream["sequence_start"] == 0
    assert stream["sequence_type"] == "bigint"
    assert stream["per_conversation_independent"] is True
    assert stream["allocation"] == "single_transaction_atomic"
    assert stream["uniqueness"] == ["conversation_id", "sequence"]

    constraints = fixture["identity_constraints"]
    # account→N tenant：账号可多 agent（会话行承载），tenant 仍全局唯一。
    assert "account_agent_tenancy" in constraints
    assert "UNIQUE" in constraints["conversation_tenant_uniqueness"]
    assert "RESTRICT" in constraints["history_cascade"]


def test_seeded_identity_matches_migration_seed() -> None:
    """fixture 的 dev 身份必须与 migration seed 固定 UUID 一致。"""
    fixture = _load_fixture()
    seeded = fixture["seeded_identity"]
    assert seeded["tenant_id"] == "dev"
    assert seeded["account_status"] == "active"
    migration_text = CANONICAL_MODULES[3].read_text(encoding="utf-8")
    assert seeded["account_id"] in migration_text
    assert seeded["conversation_id"] in migration_text


def test_canonical_modules_have_no_sqlite_or_default_tenant_path() -> None:
    """规范身份/消息实现不得出现 SQLite 回退/双写路径，不得引用 DEFAULT_TENANT。"""
    for module in CANONICAL_MODULES:
        text = module.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{module.name} 含禁止 token: {token}"
