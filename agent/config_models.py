from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bootstrap.work_queue_defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_ERROR_BACKOFF_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_ERROR_BACKOFF_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RELEASE_DELAY_SECONDS,
)
from proactive_v2.config import ProactiveConfig
from core.telemetry.retention import (
    AUDIT_DEFAULT_DAYS,
    DEBUG_CONTENT_DEFAULT_DAYS,
    OPERATIONAL_DEFAULT_DAYS,
)


@dataclass
class TelegramChannelConfig:
    token: str
    allow_from: list[str] = field(default_factory=list)
    channel_name: str = "telegram"
    api_base_url: str | None = None
    """自定义 Telegram Bot API 地址，用于国内反代等场景。
    例如：https://tg.i-c.top/bot（无需尾随 /）"""
    # C10（c10-telegram-binding-sync）：Pilot 身份绑定 + durable 同步路径。
    # 开启后：私聊消息经 telegram_identity_bindings 门禁入 canonical 流（PG
    # durable），回复经 delivery worker 投递；旧单体 allowlist 路径停用。
    # 前置：storage.backend=postgres 且 [auth].enabled（不满足启动 fail-fast）。
    # ⚠️ 同一 Bot token 的更新流不得被旧单体与 Pilot 同时消费（§5.9.2）。
    pilot_identity_binding: bool = False


@dataclass
class QQGroupConfig:
    group_id: str
    allow_from: list[str] = field(default_factory=list)
    require_at: bool = True


@dataclass
class QQChannelConfig:
    bot_uin: str
    allow_from: list[str] = field(default_factory=list)
    groups: list[QQGroupConfig] = field(default_factory=list)
    websocket_open_timeout_seconds: float = 5.0


@dataclass
class QQBotGroupConfig:
    group_openid: str
    allow_from: list[str] = field(default_factory=list)
    require_at: bool = True
    allow_proactive: bool = False


@dataclass
class ChatChannelConfig:
    enabled: bool = False
    channel_name: str = "chat"
    host: str = "127.0.0.1"
    port: int = 6322
    # §5.9.4 连接生命周期：客户端空闲读超时（秒），超时回收连接。
    idle_timeout_s: float = 90.0
    # dev-only 门禁：默认拒绝非回环绑定；P1 认证前不得公网暴露。
    allow_public_bind: bool = False


@dataclass
class ChannelsConfig:
    telegram: TelegramChannelConfig | None = None
    qq: QQChannelConfig | None = None
    socket: str = "/tmp/nexus.sock"
    cli_session_key: str = ""
    chat: ChatChannelConfig = field(default_factory=ChatChannelConfig)


@dataclass
class MemoryEmbeddingConfig:
    model: str = "text-embedding-v3"
    api_key: str = ""
    base_url: str = ""
    output_dimensionality: int | None = None


@dataclass
class MemoryConfig:
    enabled: bool = False
    engine: str = ""
    # C14 ADR-2：用户侧切换总开关（PUT /api/memory/engines/active）。关闭时
    # 用户只能查看目录，切换由管理员改配置完成；opt_in 引擎的「管理员允许」
    # = 本开关 ∧ 引擎已构建（engine 含该引擎）。
    user_engine_selection: bool = True
    embedding: MemoryEmbeddingConfig = field(default_factory=MemoryEmbeddingConfig)

    @property
    def engine_names(self) -> list[str]:
        """返回 engine 名称列表，支持逗号分隔（如 "default,rachael"）。

        向后兼容: 空值或单值按原逻辑解析，空值等价于 ["default"]。
        """
        raw = (self.engine or "").strip()
        if not raw:
            return ["default"]
        return [name.strip() for name in raw.split(",") if name.strip()]


@dataclass
class StorageConfig:
    """存储层配置：sqlite（单机兼容）或 postgres（Phase 1 目标）。"""

    backend: str = "sqlite"
    postgres_url: str = "postgresql+psycopg://nexus:nexus_dev@localhost:5433/nexus"
    """postgres 后端连接串。默认指向 docker/debug 的本地开发库（宿主端口 5433）。"""
    pool_size: int = 20


@dataclass
class CacheConfig:
    """Redis 缓存层配置。Redis 不可用时业务侧降级直连 DB（fail-open）。"""

    enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    context_ttl_seconds: int = 300  # 会话上下文 5min
    profile_ttl_seconds: int = 3600  # 用户画像 1h
    search_ttl_seconds: int = 600  # 向量检索结果 10min


@dataclass
class FitbitIntegrationConfig:
    enabled: bool = False


@dataclass
class PersonaConfig:
    identity: str = ""
    personality_rules: str = ""
    self_model: str = ""


@dataclass
class PeerAgentConfig:
    name: str
    base_url: str
    launcher: list[str]  # 拉起命令，如 ["uv", "run", "python", "-m", "app.a2a_server"]
    cwd: str | None = None  # 子进程工作目录，None 表示继承父进程
    description: str = ""  # 工具描述，用于 LLM 路由；服务器在线时会被 AgentCard 覆盖
    health_path: str = "/health"
    startup_timeout_s: int = 30
    shutdown_timeout_s: int = 10


@dataclass
class WiringConfig:
    context: str = "default"
    memory: str = "default"
    toolsets: list[str] = field(
        default_factory=lambda: [
            "meta_common",
            "spawn",
            "schedule",
            "mcp",
        ]
    )


@dataclass
class LoggingConfig:
    passive_db: str = ""
    proactive_db: str = ""
    drift_db: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.passive_db)


@dataclass
class AppServerConfig:
    enabled: bool = False
    listen: str = "127.0.0.1:2236"
    max_connections: int = 32
    ingress_queue_size: int = 128
    max_message_bytes: int = 2 * 1024 * 1024
    outbound_queue_size: int = 512


@dataclass
class AuthConfig:
    """`[auth]` 配置节（C5 design ADR-7）。

    timeout 数值 = §10 PROPOSED DEFAULT（session idle 7d / absolute 30d；
    admin idle 30min / absolute 12h；invitation ttl 7d），P-1 复核后按部署记录。
    会话创建时把数值固化到行，配置后续修改只影响新会话（design §1）。
    """

    enabled: bool = False
    """Pilot auth 总开关（Enable 步）；默认关闭 = P0.5 行为不变。"""
    cookie_secure: bool = True
    """非 dev 环境固定 Secure；本地 HTTP 显式配置为 false（§5.9.3）。"""
    origin_allowlist: list[str] = field(default_factory=list)
    """允许的前端来源（Origin/Referer 校验用 scheme+host+port）。"""
    admin_allow_ips: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    """admin HTTP/API 回环边界（ADR-5）。"""
    session_idle_hours: int = 168
    session_absolute_hours: int = 720
    admin_idle_minutes: int = 30
    admin_absolute_hours: int = 12
    invitation_token_ttl_hours: int = 168
    """邀请 Token TTL；租户邀请码同样适用（签发时固化到行）。"""
    password_min_length: int = 8
    """邮箱密码注册/登录的最小密码长度策略（注册时校验，默认 8 位）。"""

    @property
    def session_idle_s(self) -> int:
        return self.session_idle_hours * 3600

    @property
    def session_absolute_s(self) -> int:
        return self.session_absolute_hours * 3600

    @property
    def admin_idle_s(self) -> int:
        return self.admin_idle_minutes * 60

    @property
    def admin_absolute_s(self) -> int:
        return self.admin_absolute_hours * 3600


@dataclass
class AdmissionConfig:
    """C3 admission 容量初始值（§10 DECIDED 冻结数字；压测后由后续 change 调整）。"""

    global_interactive_queue: int = 128
    per_tenant_pending_interactive: int = 16
    global_maintenance_queue: int = 64
    llm_concurrency: int = 30
    embedding_concurrency: int = 4
    mcp_concurrency: int = 8
    process_concurrency: int = 2
    ws_outbound_soft_limit: int = 192
    ws_outbound_hard_limit: int = 256
    ws_outbound_max_payload_bytes: int = 1024 * 1024


@dataclass
class WorkQueueConfig:
    """C15 work item 消费层运行参数（design ADR-7 冻结初始值；可配置）。

    `enabled` 默认 **False**：接线到位但默认不启动消费者。这不只是保守——在还没有
    注册任何 `flow` handler 的部署上启动消费者，会让所有 work item 以「未注册 flow」
    计入失败并最终进死信（ADR-7 的 fail-fast 与 `<work_queue.enabled>` 共同约束）。

    `max_attempts` 上限为退避表档数（5 档：1m/5m/30m/2h/6h；字面量单一来源 =
    `bootstrap/work_queue_defaults.py`）。
    """

    enabled: bool = False
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    batch_size: int = DEFAULT_BATCH_SIZE
    maintenance_acquire_timeout_seconds: float = (
        DEFAULT_MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS
    )
    release_delay_seconds: float = DEFAULT_RELEASE_DELAY_SECONDS
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS
    max_error_backoff_seconds: float = DEFAULT_MAX_ERROR_BACKOFF_SECONDS


@dataclass
class AttachmentConfig:
    """附件内容边界（C6，openspec/changes/c6-attachment/design.md ADR-2）。

    冻结默认值 = P-1 决策；配置只在显式覆盖时改变行为，默认即冻结值。
    """

    enabled: bool = True
    max_file_bytes: int = 20 * 1024 * 1024  # 20 MiB
    # 图片资源硬上限（§5.9.15 冻结）：总像素 ≤ 4096²、解码内存 ≤64MiB、超时 5s、GIF ≤100 帧
    max_pixels: int = 4096 * 4096  # 16.8M
    max_decode_bytes: int = 64 * 1024 * 1024  # 64 MiB
    max_decode_seconds: float = 5.0
    max_gif_frames: int = 100
    max_text_chars: int = 200_000
    # 生命周期（ADR-5）：临时 staging 24h；已引用 30d（对齐 C12 30d operational 档）
    temp_ttl_hours: int = 24
    referenced_ttl_days: int = 30
    # 孤儿判定宽限（ADR-5）：rename 完成到 metadata 提交之间的在途上传不被对账删除
    orphan_grace_seconds: int = 900
    # 后台清理频率（ADR-10）：0 表示不启用周期任务（仅启动对账 + 手动触发）
    cleanup_interval_s: int = 3600
    reconcile_interval_s: int = 6 * 3600
    reconcile_enabled: bool = True
    # 是否在进程启动时对账（启动 reconciliation；dry-run 演练仍可手动）
    reconcile_on_startup: bool = True


@dataclass
class RetentionConfig:
    """数据保留期执行（p0-retention-wiring ADR-1/2/5/8）。

    三档天数默认值引用 `core.telemetry.retention` 冻结常量（单一来源）。
    `purge_grace_s` / `replay_keep_last_frames` / `replay_max_age_days` 为
    design 候选值的**暂定默认**（task 0.1 owner 确认前不视为冻结值，
    可直接改 config 调整，无需改代码）。
    """

    enabled: bool = True
    # 0 = 不启用周期任务（演练入口 run_once 仍可手动触发）
    interval_s: int = 86400
    operational_days: int = OPERATIONAL_DEFAULT_DAYS
    audit_days: int = AUDIT_DEFAULT_DAYS
    debug_content_days: int = DEBUG_CONTENT_DEFAULT_DAYS
    # 分批删除（ADR-5）：单批行数 × 单轮最大批数 = 单轮单实体删除上限
    batch_size: int = 500
    max_batches: int = 20
    # 凭据「已撤销 ∧ 已过期」后再等多久才抹摘要（design 暂记 30d）
    purge_grace_s: int = 30 * 86400
    # 重放帧按会话补发窗口（ADR-8）：每会话保留下限帧数 + 兜底年龄天花板
    replay_keep_last_frames: int = 20
    replay_max_age_days: int = 30
    # 文件 sweep root（ADR-1）：内置为空；形如 {operational = ["path"], audit = ["path"]}
    file_roots: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class PluginRuntimeConfig:
    """C8 hook failure 分层的有界 timeout（§5.9.16，task-08）。

    gate/interceptor（pre-tool、phase module）超时按 fail-closed 处置；
    fanout/telemetry（post-tool、EventBus 观察者）超时记录失败、不改终态。
    """

    hook_timeout_seconds: float = 5.0
    observer_timeout_seconds: float = 5.0


@dataclass
class Config:
    provider: str
    model: str
    api_key: str
    system_prompt: str
    max_tokens: int = 8192
    max_iterations: int = 10
    memory_window: int = 40
    base_url: str | None = None
    extra_body: dict = field(default_factory=dict)
    # 协议: "openai" = chat completions, "codex"/"responses" = OpenAI Responses API
    protocol: str = "openai"
    light_protocol: str = "openai"
    agent_protocol: str = "openai"
    vl_protocol: str = "openai"
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    memory_optimizer_enabled: bool = True
    memory_optimizer_interval_seconds: int = 64800
    light_model: str = ""
    light_api_key: str = ""
    light_base_url: str = ""
    agent_model: str = ""
    agent_api_key: str = ""
    agent_base_url: str = ""
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    fitbit: FitbitIntegrationConfig = field(default_factory=FitbitIntegrationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    multimodal: bool = True
    vl_model: str = ""
    vl_api_key: str = ""
    vl_base_url: str = ""
    tool_search_enabled: bool = False
    spawn_enabled: bool = True
    dev_mode: bool = False
    router_mode: str = "rule"
    """Router 模式: "rule" = 规则打分 + LLM 兜底, "llm" = LLM 直接路由"""
    peer_agents: list[PeerAgentConfig] = field(default_factory=list)
    wiring: WiringConfig = field(default_factory=WiringConfig)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    app_server: AppServerConfig = field(default_factory=AppServerConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    work_queue: WorkQueueConfig = field(default_factory=WorkQueueConfig)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    plugin_runtime: PluginRuntimeConfig = field(default_factory=PluginRuntimeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> Config:
        from importlib import import_module

        return import_module("agent.config").load_config(path)


__all__ = [
    "AdmissionConfig",
    "AppServerConfig",
    "AttachmentConfig",
    "AuthConfig",
    "CacheConfig",
    "ChannelsConfig",
    "ChatChannelConfig",
    "Config",
    "FitbitIntegrationConfig",
    "MemoryConfig",
    "MemoryEmbeddingConfig",
    "PeerAgentConfig",
    "PersonaConfig",
    "PluginRuntimeConfig",
    "QQChannelConfig",
    "QQBotGroupConfig",
    "QQGroupConfig",
    "RetentionConfig",
    "StorageConfig",
    "TelegramChannelConfig",
    "WiringConfig",
    "WorkQueueConfig",
]
