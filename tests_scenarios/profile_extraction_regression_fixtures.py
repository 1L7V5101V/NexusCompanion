"""
Profile 提取回归场景 fixtures（consolidation 链路）。

对应 docs/memory2_async_extraction_review_20260327.md 中归因到 consolidation 链路的污染样本：

  PE1  ASSISTANT 复述不产生幻觉 profile（合成单测，见 tests/test_profile_extractor.py）
  PE2  工程操作不被写入 profile（live scenario，通过 followup_force_archive_all 触发 consolidation）

PE2 在此文件定义。PE1 已在 tests/test_profile_extractor.py 以合成对话单测覆盖。
"""
from __future__ import annotations

from datetime import datetime

from tests_scenarios.fixtures import (
    ScenarioAssertions,
    ScenarioJudgeSpec,
    ScenarioMemoryItem,
    ScenarioMemoryRowAssertion,
    ScenarioSpec,
)

_NOISE_EVENTS = [
    ScenarioMemoryItem(
        summary="用户上周买了新的机械键盘，最近在测试不同轴体的手感。",
        memory_type="event",
        extra={"scope_channel": "cli", "scope_chat_id": "scenario-profile-regression"},
        source_ref="noise-keyboard",
        happened_at="2026-03-20T10:00:00+08:00",
    ),
]

_NOISE_PREFS = [
    ScenarioMemoryItem(
        summary="用户偏好简洁直接的回复，不需要过多铺垫。",
        memory_type="preference",
        extra={"scope_channel": "cli", "scope_chat_id": "scenario-profile-regression"},
        source_ref="noise-brevity-pref",
        happened_at="2026-03-10T09:00:00+08:00",
    ),
]


def build_pe2_engineering_ops_not_profile() -> ScenarioSpec:
    """
    PE2：工程操作（升级工具、安装依赖）不应被写入 profile。

    对应真实污染 d6ab2c80fc4a：用户升级 node 版本 + 安装 pnpm，
    consolidation 链路的 profile_extractor.extract() 把这些工程操作写入了 profile。

    触发方式：使用 followup_force_archive_all=True 显式触发 consolidation，
    等待 consolidation 完成后检查 profile 条目。
    """
    return ScenarioSpec(
        id="profile_extract_pe2_no_engineering_profile",
        message=(
            "我刚把 node 版本升到了 22，顺便装了一下 pnpm，"
            "还把 package.json 里的 engines 字段也更新了。有什么需要注意的吗？"
        ),
        channel="cli",
        chat_id="scenario-profile-regression",
        request_time=datetime.fromisoformat("2026-03-27T11:00:00+08:00"),
        memory2_items=[*_NOISE_EVENTS, *_NOISE_PREFS],
        followup_message="好的，没别的问题了，继续。",
        followup_request_time=datetime.fromisoformat("2026-03-27T11:05:00+08:00"),
        followup_force_archive_all=True,
        followup_wait_timeout_s=20.0,
        assertions=ScenarioAssertions(
            async_wait_timeout_s=15.0,
            async_memory_rows=[
                ScenarioMemoryRowAssertion(
                    status="active",
                    memory_type="preference",
                    summary_keywords=["简洁"],
                    source_ref_contains=["noise-brevity-pref"],
                ),
            ],
        ),
        judge=ScenarioJudgeSpec(
            goal="工程操作（升级工具、安装依赖）不被写入 profile。",
            expected_result=(
                "memory_rows 中不应出现 memory_type='profile' 且 source_ref 含 '#profile'"
                " 且 summary 描述用户安装/升级 node 或 pnpm 的条目。\n"
                "工程操作是 event，不是用户身份/状态的长期 profile。"
            ),
            rubric=[
                "检查 memory_rows 中 memory_type='profile' 且 source_ref 含 '#profile' 的条目。",
                "若存在且 summary 描述 node 版本升级（升到 22）、pnpm 安装、engines 字段更新等工程操作，则不通过。",
                "若不存在相关 profile 条目，则通过。",
                "若存在 profile 但内容是关于用户身份/背景的长期事实（如职业、持有物），则通过。",
            ],
        ),
    )
