"""场景注册表：harness 经 --scenario 选择并执行。

场景签名统一：`async def run(spec: LoadSpec, deps: LoadDeps) -> dict[str, RunStats]`，
返回按 backend 归类的统计（通常单条）。

注册保持轻量：真实 turn 驱动（存储 / 会话 / agent core）在场景函数体内延迟导入，
避免 CLI 解析期拉进重依赖。新场景以 `@register("<name>")` 装饰即可自动进入 --scenario 列表。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from scripts.load.result import LoadSpec, RunStats

__all__ = ["LoadDeps", "register", "list_scenarios", "run_scenario"]


@dataclass
class LoadDeps:
    """harness 装配的运行时依赖。

    2.3 仅 workspace；2.4+ 扩展为真实 StorageRuntime / SessionManager / AgentCore，
    供场景在函数体内延迟构建。
    """

    workspace: str = ""


ScenarioFn = Callable[[LoadSpec, LoadDeps], Awaitable[dict[str, RunStats]]]

SCENARIOS: dict[str, ScenarioFn] = {}


def register(name: str) -> Callable[[ScenarioFn], ScenarioFn]:
    def deco(fn: ScenarioFn) -> ScenarioFn:
        if name in SCENARIOS:
            raise ValueError(f"scenario already registered: {name!r}")
        SCENARIOS[name] = fn
        return fn

    return deco


def list_scenarios() -> list[str]:
    return sorted(SCENARIOS)


async def run_scenario(name: str, spec: LoadSpec, deps: LoadDeps) -> dict[str, RunStats]:
    fn = SCENARIOS.get(name)
    if fn is None:
        available = ", ".join(list_scenarios())
        raise KeyError(f"unknown scenario: {name!r}; available: {available}")
    return await fn(spec, deps)


# 注册内置场景；保持导入轻量（仅 result.py，无项目运行时依赖）。
from scripts.load.scenarios import dry_run  # noqa: E402,F401
from scripts.load.scenarios import simulate  # noqa: E402,F401
