from agent.loop_consolidation import _parse_consolidation_payload


def test_parse_consolidation_payload_supports_fenced_json():
    result = _parse_consolidation_payload(
        '```json\n{"history_entry":"[2026-03-09 12:00] 用户确认信息","pending_items":[{"tag":"preference","content":"不用 emoji。"}]}\n```'
    )

    assert result is not None
    assert result["history_entry"].startswith("[2026-03-09 12:00]")
    assert result["pending_items"][0]["tag"] == "preference"


def test_parse_consolidation_payload_returns_none_for_non_object():
    assert _parse_consolidation_payload('["not","object"]') is None
