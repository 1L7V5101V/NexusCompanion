"""C12 redaction 契约测试：四类脱敏命中、fail-safe、递归结构封顶。"""

from __future__ import annotations

import re

from core.telemetry.redaction import (
    CAPTURE_PLACEHOLDER,
    PatternRule,
    RedactionCategory,
    redact_text,
    redact_value,
)


def test_secret_kv_redacted_keeps_key():
    out = redact_text('api_key = "sk-abcdef1234567890abcdef"')
    assert "sk-abcdef1234567890abcdef" not in out
    assert "[REDACTED:secret]" in out
    assert out.startswith("api_key")


def test_bearer_token_redacted():
    out = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "[REDACTED:secret]" in out


def test_credential_url_redacted():
    out = redact_text("postgres://admin:p4ssw0rd@db.internal:5432/nexus")
    assert out == "postgres://[REDACTED:credential]@db.internal:5432/nexus"


def test_windows_local_path_redacted():
    out = redact_text(r"日志写入 C:\Users\hp\.nexus\x.log 失败")
    assert "C:\\Users\\hp" not in out
    assert "[REDACTED:local_path]" in out


def test_posix_and_home_paths_redacted():
    for sample in (
        "重建索引于 /home/hp/.nexus/a.log",
        "上传落在 /tmp/nexus_uploads/f.bin",
        "读取 ~/.nexus/workspace/SELF.md",
    ):
        out = redact_text(sample)
        assert "[REDACTED:local_path]" in out
        assert ".nexus" not in out


def test_pii_email_and_phone_redacted():
    out = redact_text("联系 foo.bar@example.com 或 13812345678")
    assert "foo.bar@example.com" not in out
    assert "13812345678" not in out
    assert out.count("[REDACTED:pii]") == 2


def test_non_phone_number_untouched():
    text = "订单号 12345678901 latency_ms=240"
    assert redact_text(text) == text


def test_lifecycle_metadata_untouched():
    text = (
        "work_kind=interactive flow=passive stage=memory_retrieval "
        "tenant_id=test-account-01 result=success"
    )
    assert redact_text(text) == text


def test_fail_safe_on_rule_error():
    broken = PatternRule(
        RedactionCategory.SECRET,
        re.compile("token"),
        r"\g<nonexistent_group>",
    )
    out = redact_text("token here", rules=(broken,))
    assert out == CAPTURE_PLACEHOLDER


def test_redact_value_nested_structures():
    data = {
        "msg": {"body": "email a.b@x.com"},
        "ids": [1, 2],
        "blob": b"raw",
        "n": 5,
        "flag": True,
    }
    out = redact_value(data)
    assert "a.b@x.com" not in str(out)
    assert "[REDACTED:pii]" in out["msg"]["body"]
    assert out["ids"] == [1, 2]
    assert out["blob"] == "[REDACTED:bytes:3]"
    assert out["n"] == 5
    assert out["flag"] is True


def test_redact_value_depth_cap():
    root: dict[str, object] = {}
    node = root
    for _ in range(12):
        nxt: dict[str, object] = {}
        node["child"] = nxt
        node = nxt
    node["leaf"] = "email deep@x.com"
    out = redact_value(root, max_depth=8)
    node_out: object = out
    hops = 0
    while isinstance(node_out, dict):
        node_out = node_out["child"]
        hops += 1
    # 超过 max_depth 的整层被替换为占位符（fail-safe），不再继续下钻。
    assert hops == 9
    assert node_out == CAPTURE_PLACEHOLDER


def test_redact_value_item_cap():
    big = {f"k{i}": f"email u{i}@x.com" for i in range(50)}
    out = redact_value(big, max_items=10)
    assert len(out) == 10
    redacted = sum(1 for v in out.values() if "[REDACTED:pii]" in str(v))
    assert redacted == 10
