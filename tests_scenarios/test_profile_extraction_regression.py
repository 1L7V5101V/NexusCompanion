"""
Profile 提取回归测试（consolidation 链路）。

覆盖 docs/memory2_async_extraction_review_20260327.md 中归因到 consolidation 链路的污染：
- PE1：ASSISTANT 复述不产生幻觉 profile（合成单测，见 tests/test_profile_extractor.py）
- PE2：工程操作不被写入 profile（本文件，live scenario）

运行命令：
    AKASIC_RUN_SCENARIOS=1 pytest -c pytest-scenarios.ini \
        tests_scenarios/test_profile_extraction_regression.py -q
"""
from __future__ import annotations

import os

import pytest

from tests_scenarios.profile_extraction_regression_fixtures import (
    build_pe2_engineering_ops_not_profile,
)
from tests_scenarios.scenario_runner import ScenarioRunner

_RUN_SCENARIOS = os.getenv("AKASIC_RUN_SCENARIOS") == "1"

_RUNNER = ScenarioRunner()


def _print_profile_rows(result: object) -> None:
    rows = getattr(result, "memory_rows", [])
    profile_rows = [r for r in rows if r.get("memory_type") == "profile"]
    print(f"\n[diag] profile rows ({len(profile_rows)}):")
    for r in profile_rows:
        print(f"  [{r.get('memory_type')}] {r.get('summary')}  (src={r.get('source_ref')})")


# ---------------------------------------------------------------------------
# PE2：工程操作不被写入 profile
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.scenario_profile
@pytest.mark.scenario_live
@pytest.mark.skipif(not _RUN_SCENARIOS, reason="设置 AKASIC_RUN_SCENARIOS=1 后再执行真实场景测试")
async def test_pe2_engineering_ops_not_profile() -> None:
    """
    PE2：工程操作（升级 node、安装 pnpm）不应通过 consolidation 链路写入 profile。

    真实污染（d6ab2c80fc4a）：profile_extractor.extract() 将工程操作误判为用户状态。
    修复后：_build_prompt() 的 USER-first 规则和额外禁止类型应拦截此类提取。

    测试方式：发送消息后用 followup_force_archive_all 触发 consolidation，
    等待完成后检查是否出现工程操作类 profile 条目。
    """
    spec = build_pe2_engineering_ops_not_profile()
    result = await _RUNNER.run(spec)
    _print_profile_rows(result)
    assert result.passed, result.failure_message()
