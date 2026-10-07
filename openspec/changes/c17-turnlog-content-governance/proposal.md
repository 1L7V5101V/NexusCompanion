## Why

旁路运行日志（`workspace/logs/{passive,proactive,drift}.db`）无条件保存全部对话正文，与 C12 已冻结的「message content 默认不进入任何日志、metrics、trace 或审计存储」相冲突。

现状逐条可查：`turn_logging/turn_logger.py:26` 建表为 `messages TEXT NOT NULL`，另有 `llm_response` 列；`TurnLoggerConfig` 无内容开关字段；唯一门禁 `logging_cfg.enabled` 的定义是 `bool(self.passive_db)`（`agent/config_models.py:183`），而 `config.example.toml:362-368` 三条路径全都配着——照示例配置部署即为开启。`turn_logging/` 内对 `redact` 零引用，正文未经脱敏。保留侧 `bootstrap/retention/sweeper.py:19` 自述「内置 root 集为空」，且判据为文件 mtime，对该形态既未覆盖也不可用。

单人自用时无所谓。一旦邀请外部账号，服务器上会常驻一份含全部对话正文、无清理、无 tenant 边界的本地副本，且现有日志查询界面已能读取它。所以这必须在邀请第一个人之前收口。

## What Changes

- 正文落盘加开关，**默认关闭**。关闭时结构化字段（token 数、耗时、模型名、工具调用、重试、错误、链路归属）照常完整写入，C16 的三、五层度量不受影响。
- 开启复用既有内容采集语义：管理员 principal + 开启原因 + TTL + 产生审计事件，TTL 过期与进程重启均回到默认关闭；开启期间正文入库前脱敏。不新造第二套开关。
- 旁路运行日志获得明确的保留档归属，并解决「文件 mtime 判据对持续写入的单库文件不可用」这一机制缺陷：新写入的数据按记录时间做行级裁剪（既有契约已指定该路径），不改存储布局、不按日期分片。
- **历史数据保留**：以「治理生效点之前写入的行豁免自动裁剪」实现。不迁移、不清空、不被任何周期任务删除；对其处置只能由管理员显式发起并留下审计事件。
- 顺带修正 `config.example.toml:367` 把 drift 描述为「漂流探索链路」的注释——`PILOT_ROADMAP.md` §3.1 定义 drift 为主动链的无动作分支，不是独立探索链路。

### Non-Goals

- 不删除任何历史正文。这是明确决定，不是待办。
- 不把旁路日志搬迁到 PostgreSQL（属存储整合议题，与本治理正交）。
- 不改变业务消息在 PostgreSQL 中的保存策略——`data-retention` 已明确消息保留不由该能力决定，属账号生命周期议题。
- 不新增第四保留档。
- 不实现通用行级保留框架，只为旁路日志这一种形态给出可执行路径。
- 不关闭或移除旁路日志本身：它是 C16 第三、五层度量的唯一数据源。

## Capabilities

### New Capabilities

- `turnlog-content-gate`: 旁路运行日志的内容开关契约——默认关闭、关闭态不损害非内容字段完整性、开启需三要素与审计且入库前脱敏、开关状态自身可观测但不含正文、历史数据不因开关或布局变更被批量销毁。

### Modified Capabilities

- `data-retention`: 新增旁路运行日志的保留归属需求。现有三档归属未覆盖该形态；文件 mtime 判据对持续写入的单库不可用，需明确「整库删除不得充当按天裁剪」；并明确治理机制引入时不得顺带删除既有数据。

## Impact

- **写路径**：`turn_logging/`（建表、行构造、flush）、`LoggingConfig` 配置模型、`config.example.toml`。
- **读路径**：不改存储布局，因此 `bootstrap/dashboard_api.py` 的 `LogDashboardReader` 与 C16 派生层的读取路径不变；裁剪为库内行级操作。
- **保留**：需新增「按记录时间裁剪」的执行路径——现有 sweeper 只做文件 mtime，对该形态按既有契约不可用；治理生效点作为存量豁免边界被记录并可被测试验证。
- **跨 change 依赖**：C16 的第三、五层度量依赖非内容字段在开关关闭态仍完整——本 change 的 R2 是 C16 的前向保障。
- **数据库**：无 PostgreSQL schema 变更。
- **可逆性**：开关可回退；已按新布局写入的分片文件不回收为单库。
- **风险面**：改动正在运行的写路径；关闭正文后现有日志查询页会出现空正文，需以「未采集」呈现而非空白。
