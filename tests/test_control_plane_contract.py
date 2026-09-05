"""C2 durable control plane 静态契约（无 PG 依赖，CI 可跑）。

覆盖：入站幂等双键 fixture 结构、control plane 模块无 SQLite/默认租户回退、
不 import 旧单体 bus/session 路径、delivery worker 冻结参数不变量。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bootstrap.delivery_worker import DeliveryWorkerConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "control_plane_idempotency.json"
CONTROL_PLANE_MODULES = (
    REPO_ROOT / "bootstrap" / "db" / "models" / "control_plane.py",
    REPO_ROOT / "bootstrap" / "db" / "repository" / "control_plane_repo.py",
    REPO_ROOT / "bootstrap" / "delivery_worker.py",
)


def test_idempotency_fixture_structure() -> None:
    """fixture 双键正/负用例结构冻结，供集成测试与 C4/C10 消费。"""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    keys = fixture["inbound_idempotency_keys"]
    telegram = keys["telegram"]
    webchat = keys["webchat"]
    assert telegram["fields"] == ["source_channel", "source_identity_id", "source_message_id"]
    assert webchat["fields"] == ["account_id", "client_message_id"]
    for spec in (telegram, webchat):
        assert spec["cases"], "每个键类型至少一个用例"
        for case in spec["cases"]:
            assert case["name"]
            assert isinstance(case["expect_duplicate"], bool)
            for field in spec["fields"]:
                if field == "account_id":
                    continue  # 测试以真实账号填充
                assert field in case["key"]
    # 首个用例必须是「接受成功」，且存在同键重复用例（幂等语义核心）
    for spec in (telegram, webchat):
        assert spec["cases"][0]["expect_duplicate"] is False
        assert any(case["expect_duplicate"] for case in spec["cases"])


@pytest.mark.parametrize("module_path", CONTROL_PLANE_MODULES, ids=lambda p: p.name)
def test_no_sqlite_fallback_or_default_tenant(module_path: Path) -> None:
    """control plane 模块无 SQLite 引用、无默认租户回退（§10 DECIDED）。"""
    source = module_path.read_text(encoding="utf-8")
    assert "sqlite" not in source.lower(), f"{module_path.name} 出现 sqlite 引用"
    assert "DEFAULT_TENANT" not in source, f"{module_path.name} 出现 DEFAULT_TENANT 回退"


@pytest.mark.parametrize("module_path", CONTROL_PLANE_MODULES, ids=lambda p: p.name)
def test_no_legacy_bus_or_session_imports(module_path: Path) -> None:
    """control plane 模块不接旧单体 MessageBus / SessionStore 路径（接线归 C3/C4）。"""
    source = module_path.read_text(encoding="utf-8")
    for banned in (r"^\s*from\s+bus\b", r"^\s*import\s+bus\b",
                   r"^\s*from\s+session\b", r"^\s*import\s+session\b"):
        assert not re.search(banned, source, flags=re.MULTILINE), (
            f"{module_path.name} 出现旧单体导入: {banned}"
        )


def test_delivery_worker_frozen_defaults() -> None:
    """§5.9.11 冻结初始参数：lease 60s / heartbeat 20s / 5 次退避 1m..6h。"""
    cfg = DeliveryWorkerConfig()
    assert cfg.lease_ttl_seconds == 60.0
    assert cfg.heartbeat_interval_seconds == 20.0
    assert cfg.max_attempts == 5
    assert cfg.backoff_seconds == (60.0, 300.0, 1800.0, 7200.0, 21600.0)


def test_delivery_worker_config_invariants() -> None:
    """配置不变量：lease 必须长于 heartbeat；退避表覆盖 max_attempts。"""
    with pytest.raises(ValueError):
        DeliveryWorkerConfig(lease_ttl_seconds=10.0, heartbeat_interval_seconds=20.0)
    with pytest.raises(ValueError):
        DeliveryWorkerConfig(max_attempts=0)
    with pytest.raises(ValueError):
        DeliveryWorkerConfig(max_attempts=8)  # 退避表仅 5 档
