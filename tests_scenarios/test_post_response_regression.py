"""
Post-response memory 提取回归测试。

对应 docs/memory2_async_extraction_review_20260327.md 中识别的 7 个真实污染样本，
以及 2 个正向 guard（防止过度过滤）。

运行命令：
    AKASIC_RUN_SCENARIOS=1 pytest -c pytest-scenarios.ini \
        tests_scenarios/test_post_response_regression.py -q

单条运行：
    AKASIC_RUN_SCENARIOS=1 pytest -c pytest-scenarios.ini \
        tests_scenarios/test_post_response_regression.py \
        -k test_r1_no_sentiment_migration -q
"""
from __future__ import annotations

import os

import pytest

from tests_scenarios.post_response_regression_fixtures import (
    build_p3_explicit_tool_requirement,
    build_p4_no_emoji_preference,
    build_r1_no_sentiment_migration,
    build_r2_no_correction_as_rule,
    build_r3_no_tcp_knowledge_procedure,
    build_r4_no_http2_knowledge_procedure,
    build_r5_no_skill_constraint_globalized,
    build_r6_architecture_discussion_not_procedure,
    build_r7_no_fallback_as_procedure,
)
from tests_scenarios.scenario_runner import ScenarioRunner

_RUN_SCENARIOS = os.getenv("AKASIC_RUN_SCENARIOS") == "1"

_RUNNER = ScenarioRunner()


def _print_post_response_rows(result: object) -> None:
    rows = getattr(result, "memory_rows", [])
    pr_rows = [r for r in rows if "@post_response" in str(r.get("source_ref", ""))]
    print(f"\n[diag] post_response rows ({len(pr_rows)}):")
    for r in pr_rows:
        print(f"  [{r.get('memory_type')}] {r.get('summary')}  (src={r.get('source_ref')})")


def _pr_procedures(result: object) -> list[dict]:
    """返回所有来自 @post_response 的 procedure 条目。"""
    rows = getattr(result, "memory_rows", [])
    return [
        r for r in rows
        if "@post_response" in str(r.get("source_ref", ""))
        and r.get("memory_type") == "procedure"
    ]


def _pr_preferences(result: object) -> list[dict]:
    """返回所有来自 @post_response 的 preference 条目。"""
    rows = getattr(result, "memory_rows", [])
    return [
        r for r in rows
        if "@post_response" in str(r.get("source_ref", ""))
        and r.get("memory_type") == "preference"
    ]


def _assert_noise_anchor(result: object) -> None:
    """断言噪音条目仍存在，确认 worker 正常运行。"""
    errors = getattr(result, "assertion_errors", [])
    assert not errors, f"噪音锚点断言失败（worker 可能未运行）: {errors}"


# ---------------------------------------------------------------------------
# R1：情感迁移不发生
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r1_no_sentiment_migration() -> None:
    """
    R1：对宫崎英高游戏的负面评价，不应迁移成对仁王等其他游戏的厌恶 preference。

    真实污染（8619b6e6e7a8）：用户说 Elden Ring 让人疲倦，ASSISTANT 推荐了仁王，
    worker 把"不喜欢仁王"提取为 preference。修复后 ASSISTANT 推荐不能成为证据。
    """
    spec = build_r1_no_sentiment_migration()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    migrated = [
        r for r in _pr_preferences(result)
        if any(kw in r.get("summary", "") for kw in ["仁王", "Nioh", "nioh"])
    ]
    assert not migrated, f"不应提取仁王相关厌恶 preference，但得到: {[r['summary'] for r in migrated]}"


# ---------------------------------------------------------------------------
# R2：当轮纠错不落长期记忆
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r2_no_correction_as_rule() -> None:
    """
    R2：用户当轮纠正 ASSISTANT 输出错误，不应提取为长期 procedure 或 preference。

    真实污染（3f8c576cec36）：纠错内容被提取成长期规则碎片。
    """
    spec = build_r2_no_correction_as_rule()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    all_pr = _pr_procedures(result) + _pr_preferences(result)
    # 当轮纠错不应落任何长期记忆
    assert not all_pr, f"不应提取任何 procedure/preference，但得到: {[r['summary'] for r in all_pr]}"


# ---------------------------------------------------------------------------
# R3：TCP 知识点不写成 procedure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r3_no_tcp_knowledge_procedure() -> None:
    """
    R3：用户背诵 TCP 三次握手，不应被提取为 agent procedure。

    真实污染（c3bb158fd8ac）：技术知识讲解被写成 procedure。
    """
    spec = build_r3_no_tcp_knowledge_procedure()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    bad = [
        r for r in _pr_procedures(result)
        if any(kw in r.get("summary", "") for kw in ["TCP", "握手", "SYN", "三次"])
    ]
    assert not bad, f"不应提取 TCP 知识点为 procedure，但得到: {[r['summary'] for r in bad]}"


# ---------------------------------------------------------------------------
# R4：HTTP/2 知识点不写成 procedure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r4_no_http2_knowledge_procedure() -> None:
    """
    R4：用户解释 HTTP/2 多路复用原理，不应被提取为 agent procedure。

    真实污染（ad4d0f9a954c）：协议原理被写成 procedure。
    """
    spec = build_r4_no_http2_knowledge_procedure()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    bad = [
        r for r in _pr_procedures(result)
        if any(kw in r.get("summary", "") for kw in ["HTTP/2", "HTTP2", "多路复用", "stream", "队头阻塞"])
    ]
    assert not bad, f"不应提取 HTTP/2 知识点为 procedure，但得到: {[r['summary'] for r in bad]}"


# ---------------------------------------------------------------------------
# R5：skill 局部约束不全局化
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r5_no_skill_constraint_globalized() -> None:
    """
    R5：当前 skill 局部约束不应全局化为跨任务 procedure。

    真实污染（0c362dddb990）：skill 临时约束被写成全局 agent 规则。
    """
    spec = build_r5_no_skill_constraint_globalized()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    bad = [
        r for r in _pr_procedures(result)
        if any(kw in r.get("summary", "") for kw in ["summarize", "5个", "5条", "分批", "批次"])
    ]
    assert not bad, f"不应将 skill 约束全局化为 procedure，但得到: {[r['summary'] for r in bad]}"


# ---------------------------------------------------------------------------
# R6：架构设计讨论不写成 procedure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r6_architecture_discussion_not_procedure() -> None:
    """
    R6：架构设计讨论不应被提取为全局 procedure。

    真实污染（02c74e40e47f）：主动推送插件化讨论被写成 procedure。
    """
    spec = build_r6_architecture_discussion_not_procedure()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    bad = [
        r for r in _pr_procedures(result)
        if any(kw in r.get("summary", "") for kw in ["插件化", "tick handler", "注册机制", "架构上应", "推送架构"])
    ]
    assert not bad, f"不应将架构讨论提取为 procedure，但得到: {[r['summary'] for r in bad]}"


# ---------------------------------------------------------------------------
# R7：一次性工具兜底不升级成长期 procedure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_r7_no_fallback_as_procedure() -> None:
    """
    R7：工具临时失败的一次性兜底方案不应升级为长期 procedure。

    真实污染（7badc5cfe1a0）：steam MCP 失败时用网页兜底被写成规则。
    """
    spec = build_r7_no_fallback_as_procedure()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    _assert_noise_anchor(result)
    bad = [
        r for r in _pr_procedures(result)
        if any(kw in r.get("summary", "") for kw in ["兜底", "备用", "连不上", "代替", "网页查"])
    ]
    assert not bad, f"不应将一次性兜底方案提取为 procedure，但得到: {[r['summary'] for r in bad]}"


# ---------------------------------------------------------------------------
# P3（正向 guard）：明确工具要求提取为 procedure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_p3_explicit_tool_requirement_extracted() -> None:
    """
    P3（正向）：用户明确要求使用特定工具，应正确提取为 procedure。

    保证 R1-R7 的修复没有过度过滤正常的工具使用规则。
    """
    spec = build_p3_explicit_tool_requirement()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    assert result.passed, result.failure_message()


# ---------------------------------------------------------------------------
# P4（正向 guard）：emoji 厌恶提取为 preference
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_post_response
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_p4_no_emoji_preference_extracted() -> None:
    """
    P4（正向）：用户明确不喜欢 emoji，应正确提取为 preference。

    保证 R1-R7 的修复没有过度过滤正常的偏好提取。
    """
    spec = build_p4_no_emoji_preference()
    result = await _RUNNER.run(spec)
    _print_post_response_rows(result)
    assert result.passed, result.failure_message()
