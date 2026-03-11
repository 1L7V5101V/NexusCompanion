from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.background.subagent_profiles import build_spawn_spec
from prompts.background import build_spawn_subagent_prompt


@pytest.mark.asyncio
async def test_spawn_write_file_is_scoped_to_task_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_dir = workspace / "subagent-runs" / "job-1"
    task_dir.mkdir(parents=True, exist_ok=True)

    spec = build_spawn_spec(
        workspace=workspace,
        task_dir=task_dir,
        fetch_requester=MagicMock(),
        system_prompt="test",
    )
    write_tool = next(t for t in spec.tools if t.name == "write_file")

    result = await write_tool.execute(path="final_report.md", content="done")

    assert "已写入" in result
    assert (task_dir / "final_report.md").read_text(encoding="utf-8") == "done"
    assert not (workspace / "final_report.md").exists()


def test_spawn_prompt_mentions_isolated_task_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_dir = workspace / "subagent-runs" / "job-1"

    prompt = build_spawn_subagent_prompt(workspace, task_dir)

    assert str(task_dir.resolve()) in prompt
    assert "final_report.md" in prompt
    assert "只能写入当前任务目录" in prompt
