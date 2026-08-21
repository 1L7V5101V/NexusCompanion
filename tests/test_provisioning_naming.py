"""M4H-4 稳定分区命名规则测试（commit 2，ADR §1.4）。

验证：确定性、有损 sanitize 碰撞被 hash 后缀打破、同前缀仍唯一、
标识符合法性 / 长度上界、前缀 + md5 后缀形态。
"""
import re

from infra.storage.provisioning import partition_name_for_tenant


def test_deterministic() -> None:
    assert partition_name_for_tenant("telegram:123") == partition_name_for_tenant(
        "telegram:123"
    )


def test_lossy_sanitize_collision_is_broken() -> None:
    # 旧 _sanitize_tenant 下这两个 tenant 会落到同名分区；hash 后缀必须区分。
    assert partition_name_for_tenant("telegram:123") != partition_name_for_tenant(
        "telegram_123"
    )


def test_same_readable_prefix_still_unique() -> None:
    # 前缀截断相同也由原始 tenant 的 hash 兜底唯一。
    long_a = "a" * 40 + ":x"
    long_b = "a" * 40 + ":y"
    assert partition_name_for_tenant(long_a) != partition_name_for_tenant(long_b)


def test_sanitized_prefix_identical_hash_breaks_tie() -> None:
    # 两 tenant 的 [:30] 可读前缀完全一致，唯一性仍由原始值 hash 保证。
    t1 = "x" * 30 + ":1"
    t2 = "x" * 30 + ":2"
    assert partition_name_for_tenant(t1)[: len("memory_items_") + 30] == (
        partition_name_for_tenant(t2)[: len("memory_items_") + 30]
    )
    assert partition_name_for_tenant(t1) != partition_name_for_tenant(t2)


def test_name_is_valid_identifier_and_bounded() -> None:
    for tenant in ("telegram:123", "腾讯:100000001", "a b c@d", "x" * 200):
        name = partition_name_for_tenant(tenant)
        # PG 标识符只允许 [A-Za-z0-9_]，且 ≤ NAMEDATALEN - 1 = 63。
        assert re.fullmatch(r"[A-Za-z0-9_]+", name)
        assert len(name) <= 63


def test_prefix_and_hash_shape() -> None:
    name = partition_name_for_tenant("telegram:123")
    assert name.startswith("memory_items_")
    # 末段是 12 位 md5 十六进制后缀（48-bit）。
    assert len(name.split("_")[-1]) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", name.split("_")[-1])
