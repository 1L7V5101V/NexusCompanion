"""C3 P0 出口断言：已知丢失窗口文档存在且覆盖主要进程内队列/任务。"""

from __future__ import annotations

from pathlib import Path

_LOST_WINDOWS = Path("agent/admission/LOST_WINDOWS.md")


def test_lost_windows_doc_exists_with_required_sections() -> None:
    assert _LOST_WINDOWS.exists(), "丢失窗口记录文档必须存在（P0 出口条件）"
    content = _LOST_WINDOWS.read_text(encoding="utf-8")

    # 逐项覆盖：入站队列、出站队列、lane 队列、维护意图、WS outbound、运行中 task
    required_fragments = {
        "MessageBus._inbound": "global interactive ready queue",
        "MessageBus._outbound": "outbound",
        "passive_worker": "passive_worker.py",
        "maintenance intent": "MarkdownMemoryMaintenance",
        "ws outbound": "web_chat_channel.py",
        "running task": "ConversationRuntime",
        "shell background": "shell.py",
        "replay buffer": "重放 buffer",
    }
    for name, fragment in required_fragments.items():
        assert fragment in content, f"丢失窗口文档缺少条目: {name}"

    # 每条都要写明重启行为与恢复边界（表头约束）
    for column in ("重启行为", "恢复边界"):
        assert column in content, f"丢失窗口文档缺少列: {column}"


def test_lost_windows_doc_declares_tool_unknown_boundary() -> None:
    content = _LOST_WINDOWS.read_text(encoding="utf-8")
    # 外部副作用 tool 的 unknown/compensation 边界必须显式声明（C2 前的已知缺口）
    assert "unknown" in content
    assert "compensation_required" in content
    assert "C2" in content
