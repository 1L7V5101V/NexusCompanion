from typing import Any

__all__ = ["AgentLoop"]


def __getattr__(name: str) -> Any:
    if name == "AgentLoop":
        from agent.looping.core import AgentLoop

        return AgentLoop
    raise AttributeError(name)
