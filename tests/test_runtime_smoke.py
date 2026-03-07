import json
from pathlib import Path

import pytest

import main


def _write_config(path: Path, socket_path: Path) -> None:
    payload = {
        "provider": "openai",
        "model": "test-model",
        "api_key": "test-key",
        "system_prompt": "test system prompt",
        "max_tokens": 256,
        "max_iterations": 2,
        "memory_optimizer_enabled": False,
        "proactive": {
            "enabled": False,
        },
        "channels": {
            "cli": {
                "socket": str(socket_path),
            }
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_serve_smoke_loads_config_and_runs_shutdown(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    socket_path = tmp_path / "akasic.sock"
    _write_config(config_path, socket_path)

    original_build_core_runtime = main.build_core_runtime
    observed: dict[str, object] = {}

    def _patched_build_core_runtime(config, workspace):
        runtime = original_build_core_runtime(config, workspace)
        (
            agent_loop,
            bus,
            _tools,
            _push_tool,
            _session_manager,
            scheduler,
            _provider,
            _light_provider,
            _mcp_registry,
            _memory_port,
            _presence,
        ) = runtime

        async def _agent_loop_run():
            return None

        async def _bus_dispatch_outbound():
            return None

        async def _scheduler_run():
            return None

        agent_loop.run = _agent_loop_run  # type: ignore[assignment]
        bus.dispatch_outbound = _bus_dispatch_outbound  # type: ignore[assignment]
        scheduler.run = _scheduler_run  # type: ignore[assignment]
        observed["scheduler"] = scheduler
        observed["bus"] = bus
        return runtime

    monkeypatch.setattr(main, "build_core_runtime", _patched_build_core_runtime)
    monkeypatch.setattr(main.Path, "home", lambda: tmp_path)

    await main.serve(str(config_path))

    assert socket_path.exists() is False
    assert "scheduler" in observed
    assert "bus" in observed

