"""tenant 派生 seam 测试（M4H-2 A）：可信 channel identity -> TenantContext。"""
import pytest

from infra.storage.interfaces import TenantContext
from infra.storage.tenancy import (
    DEFAULT_TENANT,
    assert_tenant_resolved,
    resolve_tenant,
    tenant_id_for_channel,
)


def test_tenant_id_for_channel_formats() -> None:
    assert tenant_id_for_channel("telegram", "123456789") == "telegram:123456789"
    assert tenant_id_for_channel("qq", "987654321") == "qq:987654321"
    assert tenant_id_for_channel("cli", "direct") == "cli:direct"


def test_tenant_id_deterministic() -> None:
    assert tenant_id_for_channel("telegram", "1") == tenant_id_for_channel(
        "telegram", "1"
    )


def test_distinct_identity_distinct_tenant() -> None:
    # 同一 channel 不同 chat_id、不同 channel 同 chat_id 都必须是不同 tenant。
    assert tenant_id_for_channel("telegram", "1") != tenant_id_for_channel("telegram", "2")
    assert tenant_id_for_channel("telegram", "1") != tenant_id_for_channel("qq", "1")


def test_resolve_tenant_returns_context() -> None:
    ctx = resolve_tenant("telegram", "42")
    assert ctx == TenantContext(tenant_id="telegram:42")


def test_assert_tenant_resolved_passes_through() -> None:
    assert assert_tenant_resolved("telegram:42") == "telegram:42"


@pytest.mark.parametrize("empty", ["", "  "])
def test_assert_tenant_resolved_fails_closed_on_empty(empty: str) -> None:
    # fail-closed：多用户路径忘记解析 tenant 必须显式报错，不静默落到 default。
    with pytest.raises(ValueError, match="tenant_id 为空"):
        assert_tenant_resolved(empty)


def test_default_tenant_constant() -> None:
    assert DEFAULT_TENANT == "default"
