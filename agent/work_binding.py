"""work 作用域 memory engine binding（C14：memory-engine-catalog ADR-3）。

每个 work 在 work start（snapshot lease 作用域内）解析一次 tenant 的 active
memory engine，并绑定到当前 task；检索管线与 memory 工具经显式穿线读取
（``TurnState.memory_engine`` → ``RetrievalRequest.engine_binding`` /
``ToolExecutionContext.memory_engine``），进行中 work 不换引擎。

ContextVar 定位与 C8 snapshot lease 一致：work 作用域运行时状态的载体，**不是
授权边界**——租户授权由 C7 ``ToolExecutionContext`` 显式承担；本变量只携带服务端
从 PG binding 解析出的引擎名（设备/模型不可写）。未绑定（dev/未接线路径）返回
空串，消费方按「回退进程 primary」处理。
"""

from __future__ import annotations

from contextvars import ContextVar

_current_engine: ContextVar[str] = ContextVar("current_work_memory_engine", default="")


def bind_work_engine(engine_id: str) -> object:
    """绑定本 work 的 active engine；返回 token 供 :func:`reset_work_engine`。"""
    return _current_engine.set((engine_id or "").strip())


def reset_work_engine(token: object) -> None:
    _current_engine.reset(token)  # type: ignore[arg-type]


def current_work_engine() -> str:
    """当前 work 的 active engine；未绑定为空串（消费方回退 primary）。"""
    return _current_engine.get()


__all__ = [
    "bind_work_engine",
    "current_work_engine",
    "reset_work_engine",
]
