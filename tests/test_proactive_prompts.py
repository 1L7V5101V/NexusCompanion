from __future__ import annotations

from types import SimpleNamespace

from prompts.proactive import build_compose_prompt_messages


def test_build_compose_prompt_messages_forbids_fabricated_links():
    ctx = SimpleNamespace(
        now_str="2026-03-18 12:00:00 CST",
        feed_text="（暂无订阅内容）",
        chat_text="用户: 5070ti能跑27b吗",
    )

    system_msg, user_msg = build_compose_prompt_messages(prompt_context=ctx)

    assert "禁止输出 example.com 这类占位链接" in system_msg
    assert "仅当上面的「今天的新内容」里明确带有真实「原文链接:」字段时" in user_msg
