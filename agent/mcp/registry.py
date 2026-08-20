"""McpServerRegistry: 管理多个 MCP server 连接，持久化到 mcp_servers.json。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agent.mcp.admin import WorkspaceMcpAdmin
from agent.mcp.client import McpClient, McpToolInfo
from agent.mcp.tool import McpToolWrapper
from agent.mcp.watcher import WorkspaceMcpWatcher
from agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agent.plugins.manager import ActivePluginInfo

logger = logging.getLogger(__name__)


class _SimpleScope:
    """PluginScope 的最小本地替代，避免导入 agent.plugins.scope。"""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id

    def defer(self, resource: str, cleanup: Any) -> None:
        pass


class _SimpleCatalog:
    """PreparedMcpCatalog 的最小本地替代。"""

    def __init__(
        self, generation_id: str, servers: dict[str, _SimpleServer]
    ) -> None:
        self.generation_id = generation_id
        self.servers = MappingProxyType(servers)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                tool.name for server in self.servers.values() for tool in server.tools
            )
        )


class _SimpleServer:
    """PreparedMcpServer 的最小本地替代。"""

    def __init__(
        self,
        name: str,
        client: McpClient,
        tools: tuple[McpToolWrapper, ...],
    ) -> None:
        self.name = name
        self.client = client
        self.tools = tools

    @property
    def remote_tool_names(self) -> tuple[str, ...]:
        return tuple(info.name for info in self.client.tool_infos)


class _SimpleGeneration:
    """WorkspaceMcpGeneration 的最小本地替代。"""

    def __init__(
        self,
        generation_id: str,
        catalog: _SimpleCatalog,
    ) -> None:
        self.generation_id = generation_id
        self.catalog = catalog
        self.revision = ""
        self.scope = _SimpleScope("workspace_mcp")
        self.runtime_snapshot = None
        self.state = "prepared"
        self.lease_count = 0


class _McpPluginManagerAdapter:
    """为 WorkspaceMcpWatcher 提供最小 PluginManager 接口的适配器。

    负责连接候选 MCP server、注册工具、支持回滚，并构建代际对象。
    """

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tool_registry = tool_registry
        self._candidate_clients: dict[str, McpClient] = {}
        self._candidate_tools: dict[str, list[str]] = {}
        self._active_clients: dict[str, McpClient] = {}
        self._active_tools: dict[str, list[str]] = {}
        self._generation_counter = 0

    async def prepare_workspace_mcp(
        self, specs: dict[str, Any], *, revision: str
    ) -> None:
        """连接候选 server，注册工具，但不晋升到 active。"""

        await self.discard_workspace_mcp_candidate()
        for name, spec in specs.items():
            if name in self._active_clients:
                continue
            client = McpClient(
                name=name,
                command=list(spec["command"]),
                env=dict(spec.get("env") or {}),
                cwd=str(spec.get("cwd") or "") or None,
            )
            await client.connect()
            self._candidate_clients[name] = client
            tools: list[str] = []
            for info in client.tool_infos:
                wrapper = McpToolWrapper(client, info, server_name=name)
                self._tool_registry.register(
                    wrapper,
                    risk="external-side-effect",
                    source_type="mcp",
                    source_name=name,
                )
                tools.append(wrapper.name)
            self._candidate_tools[name] = tools

    async def discard_workspace_mcp_candidate(self) -> None:
        """注销候选工具并断开候选连接。"""

        for name, tools in list(self._candidate_tools.items()):
            for tool_name in tools:
                self._tool_registry.unregister(tool_name)
        clients = list(self._candidate_clients.values())
        self._candidate_clients.clear()
        self._candidate_tools.clear()
        await asyncio.gather(
            *(client.disconnect() for client in clients),
            return_exceptions=True,
        )

    async def publish_workspace_mcp(self) -> _SimpleGeneration:
        """晋升候选到 active，返回代际对象。"""

        self._active_clients.update(self._candidate_clients)
        self._active_tools.update(self._candidate_tools)
        self._candidate_clients.clear()
        self._candidate_tools.clear()
        self._generation_counter += 1

        servers: dict[str, _SimpleServer] = {}
        for name, client in self._active_clients.items():
            tools = tuple(
                McpToolWrapper(client, info, server_name=name)
                for info in client.tool_infos
            )
            servers[name] = _SimpleServer(
                name=name,
                client=client,
                tools=tools,
            )

        catalog = _SimpleCatalog(
            generation_id=f"workspace-mcp-gen-{self._generation_counter}",
            servers=servers,
        )
        return _SimpleGeneration(
            generation_id=catalog.generation_id,
            catalog=catalog,
        )

    def active_servers(self) -> dict[str, McpClient]:
        """返回当前已发布的 active client 映射（用于 registry 外部查询）。"""
        return dict(self._active_clients)

    def active_tools(self) -> dict[str, list[str]]:
        """返回当前已发布的 active 工具名映射。"""
        return dict(self._active_tools)

    async def disconnect_all(self) -> None:
        """断开所有 active 和候选连接，注销全部工具。"""

        all_tools = {
            **self._active_tools,
            **self._candidate_tools,
        }
        for name, tools in all_tools.items():
            for tool_name in tools:
                self._tool_registry.unregister(tool_name)
        all_clients = {
            **self._active_clients,
            **self._candidate_clients,
        }
        self._active_clients.clear()
        self._active_tools.clear()
        self._candidate_clients.clear()
        self._candidate_tools.clear()
        await asyncio.gather(
            *(client.disconnect() for client in all_clients.values()),
            return_exceptions=True,
        )

    async def disconnect_server(self, name: str) -> None:
        """断开单个 server（用于插件 server 同步）。"""

        for tool_name in self._active_tools.pop(name, []):
            self._tool_registry.unregister(tool_name)
        for tool_name in self._candidate_tools.pop(name, []):
            self._tool_registry.unregister(tool_name)
        client = self._active_clients.pop(name, None) or self._candidate_clients.pop(
            name, None
        )
        if client is not None:
            await client.disconnect()


class McpServerRegistry:
    """管理 MCP server 连接生命周期，并将工具同步进 ToolRegistry。

    内部使用上游声明式架构：
    - WorkspaceMcpAdmin 负责 TOML 声明的 CRUD
    - WorkspaceMcpWatcher 负责轮询变更并发布代际
    - _McpPluginManagerAdapter 提供 watcher 所需的最小 PluginManager 接口

    公开接口保持与旧版一致，mcp_servers.json 仍作为兼容层保留。
    """

    def __init__(self, config_path: Path, tool_registry: ToolRegistry) -> None:
        self._config_path = config_path
        self._tool_registry = tool_registry

        # 插件直接管理的 server（不经过声明文件）
        self._clients: dict[str, McpClient] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._plugin_server_names: set[str] = set()
        self._connect_task: asyncio.Task[None] | None = None

        # 上游声明式基础设施
        self._workspace = config_path.parent
        self._mcp_root = self._workspace / "mcp"
        self._declarations_dir = self._mcp_root / "servers"

        self._plugin_adapter = _McpPluginManagerAdapter(tool_registry)
        self._watcher = WorkspaceMcpWatcher(
            manager=self._plugin_adapter,
            declarations_dir=self._declarations_dir,
            mcp_root=self._mcp_root,
        )
        self._admin = WorkspaceMcpAdmin(
            workspace=self._workspace,
            watcher=self._watcher,
        )
        self._watcher_task: asyncio.Task[None] | None = None

    async def load_and_connect_all(self) -> None:
        """启动时读取持久化配置，迁移到声明文件并启动 watcher。"""

        configs = self._load_raw_configs()
        for name, cfg in configs.items():
            try:
                await self._admin.apply(
                    name=name,
                    command=list(cfg.get("command") or []),
                    cwd=str(cfg.get("cwd") or "") or None,
                    env=dict(cfg.get("env") or {}),
                    watch_paths=[],
                )
            except Exception as e:
                logger.error("[mcp] 迁移 %r 到声明失败: %s", name, e)

        # 启动后台 watcher 轮询
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(
                self._watcher.run(),
                name="mcp_watcher",
            )

    def start_connect_all_background(self) -> None:
        """后台重连所有 server，不阻塞主服务启动。"""

        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(
                self.load_and_connect_all(),
                name="mcp_connect_all",
            )

    async def shutdown(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass

        # 停止 watcher
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher.stop()
            try:
                await asyncio.wait_for(self._watcher.wait_stopped(), timeout=10.0)
            except asyncio.TimeoutError:
                self._watcher_task.cancel()
                try:
                    await self._watcher_task
                except asyncio.CancelledError:
                    pass

        # 断开插件 server
        clients = list(self._clients.values())
        self._clients.clear()
        self._server_tools.clear()
        self._plugin_server_names.clear()
        await asyncio.gather(
            *(client.disconnect() for client in clients),
            return_exceptions=True,
        )

        # 断开声明式 server
        await self._plugin_adapter.disconnect_all()

    async def sync_plugin_servers(
        self,
        active_plugins: list[Any],
    ) -> None:
        desired: dict[str, dict[str, Any]] = {}
        for plugin in active_plugins:
            for server_name, config in plugin.mcp_servers.items():
                if server_name in desired:
                    logger.warning(
                        "[mcp] 插件 MCP server 名称冲突，保留第一项: %s", server_name
                    )
                    continue
                desired[server_name] = config

        for server_name in sorted(self._plugin_server_names - desired.keys()):
            await self._disconnect_server(server_name)
            self._plugin_server_names.discard(server_name)

        for server_name in sorted(desired.keys() - self._plugin_server_names):
            if server_name in self._clients:
                logger.warning("[mcp] 插件 MCP server 已存在，跳过: %s", server_name)
                continue
            config = desired[server_name]
            try:
                await self._connect(
                    server_name,
                    list(config.get("command") or []),
                    dict(config.get("env") or {}),
                    str(config.get("cwd") or "") or None,
                )
            except Exception as e:
                logger.warning("[mcp] 插件 MCP server 启动失败 (%s): %s", server_name, e)
                continue
            self._plugin_server_names.add(server_name)

    async def add(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        # 检查是否已存在（声明式或插件式）
        declaration_path = self._declarations_dir / f"{name}.toml"
        if declaration_path.exists() or name in self._clients:
            return f"MCP server {name!r} 已存在。如需更新，请先 mcp_remove 再重新添加。"
        try:
            result = await self._admin.apply(
                name=name,
                command=command,
                cwd=cwd,
                env=dict(env or {}),
                watch_paths=[],
            )
            self._save_mcp_servers_json()
            runtime = result.get("runtime", {})
            tools = runtime.get("tools", [])
            return (
                f"已连接 MCP server {name!r}，注册了 {len(tools)} 个工具：\n"
                + "\n".join(f"- {n}" for n in tools)
            )
        except Exception as e:
            return f"连接 MCP server {name!r} 失败：{e}"

    async def remove(self, name: str) -> str:
        # 先检查是否是声明式管理的 server
        declaration_path = self._declarations_dir / f"{name}.toml"
        if declaration_path.exists():
            try:
                await self._admin.remove(name)
                self._save_mcp_servers_json()
                return f"已注销 MCP server {name!r}。"
            except Exception as e:
                return f"注销 MCP server {name!r} 失败：{e}"

        # 再检查是否是插件直接管理的 server
        if name not in self._clients:
            all_names = list(self._plugin_adapter.active_servers()) + list(
                self._clients.keys()
            )
            return f"MCP server {name!r} 不存在，当前已注册：{all_names or '无'}"

        await self._disconnect_server(name)
        self._plugin_server_names.discard(name)
        self._save_mcp_servers_json()
        return f"已注销 MCP server {name!r}。"

    def list_servers(self) -> str:
        lines: list[str] = []

        # 声明式 server
        status = self._watcher.status()
        active_servers = status.get("servers", [])
        active_tools = status.get("tools", [])
        for name in active_servers:
            server_tools = [t for t in active_tools if t.startswith(f"mcp_{name}__")]
            lines.append(
                f"- {name}（{len(server_tools)} 个工具）：{', '.join(server_tools) or '无'}"
            )

        # 插件直接管理的 server
        for name in self._clients:
            tools = self._server_tools.get(name, [])
            lines.append(
                f"- {name}（{len(tools)} 个工具）：{', '.join(tools) or '无'}"
            )

        if not lines:
            return "当前没有已注册的 MCP server。"
        return "\n".join(lines)

    async def _connect(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None,
        cwd: str | None = None,
    ) -> list[str]:
        client = McpClient(name=name, command=command, env=env, cwd=cwd)
        tool_infos = await client.connect()
        tool_names = []
        for info in tool_infos:
            wrapper = McpToolWrapper(client, info)
            self._tool_registry.register(
                wrapper,
                risk="external-side-effect",
                source_type="mcp",
                source_name=name,
            )
            tool_names.append(wrapper.name)
        self._clients[name] = client
        self._server_tools[name] = tool_names
        return tool_names

    def _load_raw_configs(self) -> dict[str, Any]:
        if not self._config_path.exists():
            return {}
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            return data.get("servers", {})
        except Exception as e:
            logger.warning("[mcp] 读取配置失败 %s: %s", self._config_path, e)
            return {}

    def _save_mcp_servers_json(self) -> None:
        """将当前声明式 server 状态同步回 mcp_servers.json 以保持兼容。"""

        servers: dict[str, dict[str, Any]] = {}
        for name, client in self._plugin_adapter.active_servers().items():
            servers[name] = {
                "command": client.command,
                "env": client.env,
                "cwd": client.cwd,
            }
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[mcp] 保存配置失败: %s", e)

    async def _disconnect_server(self, name: str) -> None:
        for tool_name in self._server_tools.pop(name, []):
            self._tool_registry.unregister(tool_name)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.disconnect()
