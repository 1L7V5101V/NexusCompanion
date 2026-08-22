from __future__ import annotations

from pathlib import Path

from agent.plugins.doctor import format_plugin_doctor_report, run_plugin_doctor
from agent.plugins.manifest import upsert_plugin_manifest

_PLUGIN_PY = (
    "from agent.plugins import Plugin\n"
    "class DemoPlugin(Plugin):\n"
    "    name = 'demo'\n"
    "    @classmethod\n"
    "    def skill_roots(cls):\n"
    "        return ('skills',)\n"
)


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        'provider = "openai"\nmodel = "test"\n[memory]\nengine = "default"\n',
        encoding="utf-8",
    )
    return config


def test_plugin_doctor_reports_healthy_skill_plugin(tmp_path: Path) -> None:
    plugins_home = tmp_path / ".nexus-plugin"
    workspace = tmp_path / "workspace"
    plugin_root = plugins_home / "cache" / "github" / "demo" / "0.1.0"
    skill_dir = plugin_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n---\nbody\n",
        encoding="utf-8",
    )
    (plugin_root / "plugin.py").write_text(_PLUGIN_PY, encoding="utf-8")
    (workspace / "skills").mkdir(parents=True)
    (workspace / "skills" / "demo-skill").symlink_to(skill_dir, target_is_directory=True)
    upsert_plugin_manifest("demo@github", enabled=True, plugins_home=plugins_home)

    report = run_plugin_doctor(
        plugin_id="demo@github",
        plugins_home=plugins_home,
        workspace=workspace,
        config_path=str(_write_config(tmp_path)),
    )

    assert report["status"] == "healthy"
    checks = {item["name"]: item for item in report["plugins"][0]["checks"]}
    assert checks["install"]["status"] == "ok"
    assert checks["skills"]["status"] == "ok"


def test_plugin_doctor_reports_unlinked_skill_as_degraded(tmp_path: Path) -> None:
    plugins_home = tmp_path / ".nexus-plugin"
    workspace = tmp_path / "workspace"
    plugin_root = plugins_home / "cache" / "github" / "demo" / "0.1.0"
    skill_dir = plugin_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n---\nbody\n",
        encoding="utf-8",
    )
    (plugin_root / "plugin.py").write_text(_PLUGIN_PY, encoding="utf-8")
    # 已启用但未建 workspace 软链接 → skills warn
    upsert_plugin_manifest("demo@github", enabled=True, plugins_home=plugins_home)

    report = run_plugin_doctor(
        plugin_id="demo@github",
        plugins_home=plugins_home,
        workspace=workspace,
        config_path=str(_write_config(tmp_path)),
    )

    assert report["status"] == "degraded"
    checks = {item["name"]: item for item in report["plugins"][0]["checks"]}
    assert checks["policy"]["status"] == "ok"
    assert checks["skills"]["status"] == "warn"
    text = format_plugin_doctor_report(report)
    assert "plugin doctor demo@github" in text
