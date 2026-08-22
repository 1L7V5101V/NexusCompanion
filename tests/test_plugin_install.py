from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import agent.plugins.install as install_module
from agent.plugins.install import install_git_plugin


def test_install_git_plugin_installs_into_cache_and_preserves_data(tmp_path: Path) -> None:
    repo = tmp_path / "feed-mcp"
    repo.mkdir(parents=True)
    (repo / "plugin.py").write_text(
        "from agent.plugins import Plugin\n"
        "\n"
        "class Feed(Plugin):\n"
        "    name = 'feed'\n"
        "    version = '0.1.0'\n",
        encoding="utf-8",
    )
    (repo / "skills" / "feed-manage").mkdir(parents=True)
    (repo / "skills" / "feed-manage" / "SKILL.md").write_text(
        "---\nname: feed-manage\ndescription: feed\n---\nbody\n",
        encoding="utf-8",
    )

    _run_git(["init"], cwd=repo)
    _run_git(["config", "user.name", "test"], cwd=repo)
    _run_git(["config", "user.email", "test@example.com"], cwd=repo)
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-m", "init"], cwd=repo)

    home = tmp_path / "plugins-home"
    data_dir = home / "data" / "feed-lab"
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text('{"keep":true}\n', encoding="utf-8")

    result = install_git_plugin(
        source=str(repo),
        marketplace="lab",
        plugins_home=home,
    )

    assert result.plugin_name == "feed"
    assert result.plugin_version == "0.1.0"
    assert result.installed_path == home / "cache" / "lab" / "feed" / "0.1.0"
    assert (result.installed_path / "plugin.py").exists()
    assert (result.installed_path / "skills" / "feed-manage" / "SKILL.md").exists()
    assert (result.data_path / "state.json").read_text(encoding="utf-8").strip() == '{"keep":true}'
    manifest = tomllib.loads((home / "manifest.toml").read_text(encoding="utf-8"))
    assert manifest["plugins"]["feed@lab"]["enabled"] is True


def test_install_git_plugin_prepares_mcp_venv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "feed-mcp"
    repo.mkdir(parents=True)
    (repo / "plugin.py").write_text(
        "from agent.plugins import Plugin\n"
        "from agent.plugins.specs import McpServerSpec\n"
        "\n"
        "class Feed(Plugin):\n"
        "    name = 'feed'\n"
        "    version = '0.1.0'\n"
        "\n"
        "    @classmethod\n"
        "    def mcp_servers(cls):\n"
        "        return [McpServerSpec(name='feed', command=('python', 'mcp/run_mcp.py'), env={}, cwd='.')]\n",
        encoding="utf-8",
    )
    (repo / "mcp").mkdir(parents=True)
    (repo / "mcp" / "run_mcp.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "mcp" / "requirements.txt").write_text("requests\n", encoding="utf-8")

    _run_git(["init"], cwd=repo)
    _run_git(["config", "user.name", "test"], cwd=repo)
    _run_git(["config", "user.email", "test@example.com"], cwd=repo)
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-m", "init"], cwd=repo)

    calls: list[tuple[str, Path]] = []

    def _fake_run(args: list[str], *, cwd: Path, label: str) -> None:
        calls.append((label, cwd))
        if label.endswith("venv"):
            python_path = install_module._venv_python_path(cwd / ".venv")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(install_module, "_run_command", _fake_run)

    result = install_git_plugin(
        source=str(repo),
        marketplace="lab",
        plugins_home=tmp_path / "plugins-home",
    )

    assert [label for label, _ in calls] == ["feed venv", "feed pip install"]
    assert all(cwd.name == "mcp" for _, cwd in calls)
    assert (result.installed_path / "mcp" / "run_mcp.py").exists()
    expected_python = install_module._venv_python_path(
        result.installed_path / "mcp" / ".venv"
    )
    assert expected_python.exists()
    manifest = tomllib.loads(
        ((tmp_path / "plugins-home") / "manifest.toml").read_text(encoding="utf-8")
    )
    assert manifest["plugins"]["feed@lab"]["enabled"] is True


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    if result.returncode == 0:
        return
    raise AssertionError(result.stderr or result.stdout)
