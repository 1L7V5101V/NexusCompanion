"""
tool_search / list_tools 路由行为 baseline。

这套测试是衡量"改动前 vs 改动后"的基准线：
- 改动前跑：预期部分失败（记录当前行为缺陷）
- 改动后跑：预期全部通过（验证改动有效）

五个场景分别覆盖：
  S1  unknown_function       功能描述型请求 → 最终调用 schedule，不得 list_tools
  S2  direct_call            已可见工具 → 直接调用，不得先搜索
  S3  capability_query_meta  宏观能力查询 → 只走元工具，不误执行业务工具
  S4  rss_management         生僻功能 → 必须找到 feed_manage，不得拒绝
  S5  removed_tool_self_heal 旧工具名失效 → query hint 自愈到 feed_manage
"""

from __future__ import annotations

import os

import pytest

from tests_scenarios.fixtures import (
    build_history_hit_removed_tool_self_heal_scenario,
    build_tool_routing_capability_overview_scenario,
    build_tool_routing_direct_call_scenario,
    build_tool_routing_rss_management_scenario,
    build_tool_routing_unknown_function_scenario,
)
from tests_scenarios.scenario_runner import ScenarioRunner

_RUN_SCENARIOS = os.getenv("AKASIC_RUN_SCENARIOS") == "1"
_SKIP = pytest.mark.skipif(
    not _RUN_SCENARIOS,
    reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试",
)


@pytest.mark.asyncio
@pytest.mark.scenario_routing
@pytest.mark.scenario_live
@_SKIP
async def test_routing_unknown_function_goes_through_tool_search() -> None:
    """S1: 功能描述型请求（不知道工具名）→ 必须经 tool_search 解锁 schedule，不得调 list_tools。

    当前 prompt 缺少三段式路由说明时，模型可能直接调 list_tools 逐一扫描，
    或者干脆说"没有这个能力"。改动三/一生效后预期通过。
    """
    spec = build_tool_routing_unknown_function_scenario()
    runner = ScenarioRunner()
    result = await runner.run(spec)
    assert result.passed, result.failure_message()


@pytest.mark.asyncio
@pytest.mark.scenario_routing
@pytest.mark.scenario_live
@_SKIP
async def test_routing_visible_tool_called_directly_without_search() -> None:
    """S2: already-visible 的 always-on 工具 → 模型应直接调用，不得先调 tool_search 或 list_tools。

    这是性能基准：如果改动后模型仍先搜索再调用 web_search，说明 description 引导过度。
    改动前若模型已足够聪明，此测试也应通过；若失败则说明 description 有歧义。
    """
    spec = build_tool_routing_direct_call_scenario()
    runner = ScenarioRunner()
    result = await runner.run(spec)
    assert result.passed, result.failure_message()


@pytest.mark.asyncio
@pytest.mark.scenario_routing
@pytest.mark.scenario_live
@_SKIP
async def test_routing_capability_query_meta_only() -> None:
    """S3: 宏观能力查询 → 只走元工具（list_tools/tool_search），禁止误执行具体业务工具。

    用户明确说"先别执行，只列给我看"。
    相比旧 S3 增加了 forbidden_tools 约束（fitbit_health_snapshot / schedule / feed_manage），
    不再依赖 judge 判断是否误执行，变为硬断言。
    """
    spec = build_tool_routing_capability_overview_scenario()
    runner = ScenarioRunner()
    result = await runner.run(spec)
    assert result.passed, result.failure_message()


@pytest.mark.asyncio
@pytest.mark.scenario_routing
@pytest.mark.scenario_live
@_SKIP
async def test_routing_removed_tool_self_heal() -> None:
    """S5: 旧工具名 rss_add 不存在 → query hint 引导 → tool_search 自愈 → feed_manage 执行。

    端到端验证改动二（query hint）的完整链路：
    rss_add 不存在 → 错误消息含 "rss add" hint → tool_search → feed_manage。
    max_tool_calls={"tool_search": 2} 防止无限搜索循环。
    """
    spec = build_history_hit_removed_tool_self_heal_scenario()
    runner = ScenarioRunner()
    result = await runner.run(spec)
    assert result.passed, result.failure_message()


@pytest.mark.asyncio
@pytest.mark.scenario_routing
@pytest.mark.scenario_live
@_SKIP
async def test_routing_rss_management_reaches_feed_manage() -> None:
    """S4: 生僻功能（RSS 订阅管理）→ 模型必须找到 feed_manage，不得以"没有能力"拒绝。

    核心测量：改动前 direct_refusal_rate 高，改动后通过 tool_search 自愈。
    若模型幻觉调用 rss_add 等不存在工具，query hint（改动二）应引导其恢复。
    """
    spec = build_tool_routing_rss_management_scenario()
    runner = ScenarioRunner()
    result = await runner.run(spec)
    assert result.passed, result.failure_message()
