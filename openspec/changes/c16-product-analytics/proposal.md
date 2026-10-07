## Why

系统目前只能回答「服务有没有活」，回答不了「有没有人回来用、三条链到底有没有工作、一次 Agent 运行花了多少」。

已有的观测能力全部面向运维：`observability-load` 提供 turn 追踪与指标导出，`c12-observability-backup` 定义采集的隐私边界。仓库内没有产品度量视图，也没有把已有事实组织成产品口径。

调查后确认一个此前没意识到的事实：**最丰富的那份运行数据已经在采集，只是不在受控读路径上。** `workspace/logs/` 下有 `passive.db`、`proactive.db`、`drift.db` 三个 SQLite（`bootstrap/dashboard_api.py` 的 `LogDashboardReader` 已在读），字段含 turn 类型、模型名、输入/输出/缓存命中 token 数、工具调用、重试、耗时、错误。也就是说「Agent 一次运行干了多少活、花了多少 token」今天已经落盘，缺的是口径、聚合、访问控制与保留治理，不是采集。

时机上，C1/C2/C5/C6/C9/C10/C15 已把账号、认证、附件、persona、跨端绑定、投递与尝试落进 PostgreSQL，试用即将开始。再晚，早期使用数据永久缺失且无法补采。

## What Changes

度量按五层组织，北极星指标在最上层。

**第一层 用户有没有真的在用**：日/周/月活跃账号数、回访（次日/7 日/30 日）、对话开启次数与人均、每次开启的消息轮次与对话深度、用户中止率。

**第二层 三条链有没有工作**：passive（触发数、成功数、耗时、用户是否继续）、proactive（排程数、执行数、投递成功数、回应数、继续对话数、无动作分支占比）。drift 按 `PILOT_ROADMAP.md` §3.1 是 proactive 的无动作分支，不单列为一条链。

**第三层 一次运行干了多少活**：每次 turn 的步数、模型调用数、工具调用数、检索调用数、重试次数、失败工具数、最大执行深度、排队等待、模型耗时、工具耗时；步数与工具调用数给出 P50/P95 与随时间的漂移。

**第四层 记忆到底有没有用**：召回触发数、实际召回数、注入条数、被模型引用数、冲突数，以及「有记忆参与」与「无记忆参与」两类 turn 上负反馈率的差异。

**第五层 成本与性能**：每次 turn / 每会话 / 每账号每日的 token 数与估算成本；每「有效交互」的成本；后台自主任务（用户未主动发起的 turn）单独归集；端到端延迟分位数。

**北极星 Useful Interaction Rate**：由用户对回复的显式评价（赞 / 踩）判定，不读取任何会话正文。未被评价的交互不推断为有效或无效，按「已评价」与「未评价」分列呈现。

**逐层下钻**：概览数字可追到账号 → 会话 → turn → 阶段 → 具体失败节点。本项目的运行单位是 turn（`turn_id`），不新造 `run_id` 别名。

### 数据源与采集边界

- 主体为派生：从 PostgreSQL 事实表与既有 `turn_logs` 旁路库读取，不重建采集管道。
- 新增采集三处：消息级显式反馈（赞/踩）、记忆召回痕迹（标识与计数）、流式首字耗时。其余缺口登记不补。
- 内容边界：产品度量 SHALL NOT 读取或复制会话正文来判定质量。质量信号一律来自用户显式评价或可观测行为，不来自文本分析。业务库本就保存正文以支撑对话与记忆，本 change 不因度量目的新增任何正文读取路径。
- 跨库下钻（PostgreSQL 与 `turn_logs`）走受控只读路径，不合并存储。

### Non-Goals

- 不接任何第三方托管分析（PostHog、GA、Umami、Sentry 等），不引入外部上报依赖。
- 不做 A/B 实验框架、用户分群、同期群回访曲线平台。
- 不做实时流式计算；度量按需或周期批量派生。
- **不修改 `turn_logs` 当前无条件持久化全量消息内容这一既有缺陷**——它属于隐私契约对齐修复，另开 fix change 处理。本 change 只依赖其现状，不扩大其内容面。
- 不为 `autonomous_exploration` 提供度量：该 Pipeline 不存在，指标无对象。
- 不实现「主动推送被打开」这一跳：需要客户端已读回执，会改动 `c4` 冻结的协议 fixture，属独立决策。推送质量改由该条消息上的显式评价度量。
- 不做文本语义判定：不调用模型或规则去读用户消息以推断满意、纠正或情绪。
- 不在即时通讯通道提供反馈按钮：该通道当前无内联按钮用法，需另行接入；本期反馈仅覆盖网页对话通道。
- 不定义 SLO 或红线阈值；沿用 C12「基线报告前不冻结」，本产品度量服务于基线报告本身。
- 不改数据保留契约：不新增第四保留档，不延长任何源数据的保留期。

## Capabilities

### New Capabilities

- `product-analytics`: 五层产品与 Agent 运行度量所需的可验证契约——派生优先与新采集白名单、口径单一来源（含日/周/月窗口）、账号键控数据的访问控制与审计、与运维指标导出面的隔离、小样本输出形态、显式评价采集与未评价样本不外推、不读正文的内容边界、运行过程与成本口径、记忆有效性口径、逐层下钻链路。

> 结构说明：第三、四层（运行过程与记忆有效性）严格说属于 Agent runtime 观测，不是产品分析。当前折进同一 capability 以保持单一规格文件；若拆为独立 capability，需新建 spec 文件。

### Modified Capabilities

无。本 change 不修改任何已进入 `openspec/specs/` 的需求：

- 与 `observability-load` 的关系是「只读复用其追踪与导出面」，不改变其契约；
- 与 `data-retention` 的关系是「派生不延长源数据保留」，不改变三档归属；
- 与 `observability-privacy`（C12 增量，尚未 sync 进主 specs）的关系是「本侧不申请带账号维度的 label、不扩大内容采集面」，不修改其白名单，也不依赖其先 sync 才能落地。

## Impact

- **读侧**：新增派生查询模块。PostgreSQL 侧对象为 `canonical_messages`、`canonical_conversations`、`turns`、`tool_calls`、`auth_sessions`、`access_tokens`、`inbox_records`、`outbound_delivery_intents`、`delivery_attempts`、`telegram_identity_bindings`、`attachments`、`message_attachments`、`persona_audit_events`、`memory_items`；旁路侧为 `workspace/logs/{passive,proactive,drift}.db` 的 `turn_logs`。
- **写侧**：三处新增采集——消息级显式评价、记忆召回痕迹（标识与计数）、流式首字耗时。评价走普通 HTTP 端点，不新增对话帧类型，因此不触碰已冻结的协议 fixture。
- **数据库**：新增一张消息级评价表（expand-only alembic revision，外键指向规范化消息）；记忆痕迹复用旁路日志既有 metadata 列，无迁移。
- **配置**：新增模型价格表配置项（无价模型必须显式标注为「成本不可估算」，不得静默计零）。
- **前端**：网页对话通道的每条回复增加评价控件，含改评与撤销。
- **接口**：Dashboard 只读产品视图与对应管理端点，受既有管理员鉴权约束。
- **依赖**：无新增第三方依赖。`turn_logs` 内容治理已由 change `c17-turnlog-content-governance` 承接，是本 change 的**后续**而非前置——不阻塞实施。C17 的 D5 已把该库关闭态的保留归属定为 operational 30 天，以支撑本 change 的月窗口聚合。
- **风险面**：跨库下钻与旁路 SQLite 扫描需限定有界时间窗与只读；`turn_logs` 无保留治理，视图必须能显示其数据覆盖起点，避免把「没有数据」读成「没有发生」。
