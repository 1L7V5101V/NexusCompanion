from agent.loop_consolidation import _format_pending_items


def test_format_pending_items_keeps_allowed_tags_only():
    text = _format_pending_items(
        [
            {"tag": "identity", "content": "北工大软工（都柏林）233721 班，大三。"},
            {"tag": "preference", "content": "不用 emoji。"},
            {"tag": "unknown", "content": "should be dropped"},
            {"tag": "", "content": "empty tag should be dropped"},
            {"tag": "requested_memory", "content": ""},
        ]
    )

    assert "- [identity] 北工大软工（都柏林）233721 班，大三。" in text
    assert "- [preference] 不用 emoji。" in text
    assert "should be dropped" not in text


def test_format_pending_items_deduplicates_and_normalizes_tags():
    text = _format_pending_items(
        [
            {"tag": "Preference", "content": "不要在非游戏话题强行套游戏比喻。"},
            {"tag": "preference", "content": "不要在非游戏话题强行套游戏比喻。"},
            {"tag": "health_long_term", "content": "有长期贫血情况。"},
        ]
    )

    assert text.count("- [preference] 不要在非游戏话题强行套游戏比喻。") == 1
    assert "- [health_long_term] 有长期贫血情况。" in text
