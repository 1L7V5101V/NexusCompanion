"""C8 P2 dormant 安装测试（task-08 2.5，§5.9.16）。

冻结决策：安装时只把 package/manifest 登记进 catalog，不执行插件代码；
Python class/decorator handler 的代码级注册必须在候选激活时 import 插件后
发生。插件保持 dormant（未挂任何 tenant binding）时不进入任何
``TenantRuntimePlan``（plan 过滤语义见 test_tenant_runtime_plan）。

覆盖：manifest 登记（upsert_plugin_manifest）不触发插件 import（插件模块的
import 副作用——标记文件——不出现）；PluginManager.load_all()（候选激活）才
真正 import 并产生该副作用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugins.manifest import load_plugin_manifest, upsert_plugin_manifest

_PLUGIN_NAME = "c8_dormant"


def _write_plugin_with_import_side_effect(plugin_dir: Path, marker: Path) -> None:
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        "from agent.plugins import Plugin\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        f"class Dormant(Plugin):\n"
        f"    name = {_PLUGIN_NAME!r}\n"
        "    version = '0.1.0'\n",
        encoding="utf-8",
    )


def test_manifest_registration_does_not_execute_plugin_code(tmp_path: Path) -> None:
    """安装 = manifest 登记：写目录 + upsert manifest 后，插件代码未执行。"""
    marker = tmp_path / "import-marker.txt"
    plugin_dir = tmp_path / "plugins" / _PLUGIN_NAME
    _write_plugin_with_import_side_effect(plugin_dir, marker)

    # 安装登记：manifest upsert 只写 catalog 文件
    upsert_plugin_manifest(f"{_PLUGIN_NAME}@lab", enabled=True, plugins_home=tmp_path)
    entries = load_plugin_manifest(tmp_path)
    assert entries.get(f"{_PLUGIN_NAME}@lab") is True

    # dormant：代码未 import、import 副作用未发生
    assert not marker.exists()


@pytest.mark.asyncio
async def test_activation_imports_plugin_code(tmp_path: Path) -> None:
    """候选激活（PluginManager.load_all）才执行插件代码（import 副作用发生）。"""
    from agent.plugins.manager import PluginManager
    from agent.tools.registry import ToolRegistry
    from bus.event_bus import EventBus

    marker = tmp_path / "import-marker.txt"
    plugins_root = tmp_path / "plugins"
    plugin_dir = plugins_root / _PLUGIN_NAME
    _write_plugin_with_import_side_effect(plugin_dir, marker)

    upsert_plugin_manifest(f"{_PLUGIN_NAME}@lab", enabled=True, plugins_home=tmp_path)
    assert not marker.exists()  # 激活前保持 dormant

    manager = PluginManager(
        plugin_dirs=[plugins_root],
        event_bus=EventBus(),
        tool_registry=ToolRegistry(),
    )
    await manager.load_all()
    # 激活后：代码级注册发生（import 副作用 + 进入 manager catalog）
    assert marker.exists()
    assert _PLUGIN_NAME in {m["name"] for m in manager.discover()}
