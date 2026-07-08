from __future__ import annotations

from pathlib import Path

from agent.config_models import Config
from core.memory.plugin import MemoryPluginBuildDeps, MemoryPluginRuntime
from plugins.rachael.config import (
    ensure_rachael_config_file,
    load_rachael_config,
    resolve_rachael_db_path,
)
from plugins.rachael.engine import RachaelMemoryEngine


class MemoryPlugin:
    plugin_id = "rachael"

    # 准备 Rachael sidecar 存储。
    def ensure_workspace_storage(
        self,
        *,
        config: Config,
        workspace: Path,
    ) -> list[tuple[Path, bool]]:
        # 1. 确保插件配置存在，并按配置解析数据库路径。
        _ = config
        _ = ensure_rachael_config_file()
        rachael_config = load_rachael_config()
        db_path = resolve_rachael_db_path(
            workspace=workspace,
            rachael_config=rachael_config,
        )
        existed = db_path.exists()

        # 2. 创建 schema 后返回给启动日志展示。
        RachaelMemoryEngine.ensure_workspace_storage(
            rachael_config=rachael_config,
            workspace=workspace,
        )
        return [(db_path, existed)]

    # 构造 Rachael memory runtime。
    def build(
        self,
        deps: MemoryPluginBuildDeps,
    ) -> MemoryPluginRuntime:
        # 1. Rachael 是独立 memory engine，不继承 default_memory 的 store/retriever。
        rachael_config = load_rachael_config()
        engine = RachaelMemoryEngine(
            config=deps.config,
            rachael_config=rachael_config,
            workspace=deps.workspace,
            http_resources=deps.http_resources,
            event_publisher=deps.event_publisher,
        )
        return MemoryPluginRuntime(
            engine=engine,
            closeables=list(engine.closeables),
            admin=engine,
        )
