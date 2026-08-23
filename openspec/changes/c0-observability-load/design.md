# C0 可观测性与负载工具 — 设计

## Context

C1 Storage Foundation 已 merge（`0a83314d`）：`StorageRuntime.for_tenant` 提供 sqlite/postgres 双后端 tenant-bound view（`infra/storage/runtime.py:98`），config `storage.backend` 切换（`agent/config.py:295`，`infra/storage/factory.py:48/65/84`），`test_storage_parity` 已按双后端参数化。动机见 proposal.md - Why。

已存在可复用基础：
- **turn 诊断**：`core/common/diagnostic_log.py` 的 `diagnostic_context`/`diagnostic_line` 已贯穿 `PassiveTurnPipeline` 各 phase（`agent/core/passive_turn.py:371-596`，含 before_turn/before_reasoning/reasoner/after_reasoning/after_turn + turn_id）；`turn_logging.RoutingTurnLogger` 运行时组装；`bus/events_lifecycle.py` 生命周期事件携带 `turn_id`，`bootstrap/control_execution.py` 已按 turn_id 关联事件。
- **dashboard**：`bootstrap/dashboard_api.py` 只读 SQLite workspace DB（`get_overview`/`list_logs`/`get_log_detail`）与插件面板，无实时指标源。
- **bench 现状**：仅 `tests/provision_5000_bench.py`（provisioning DDL 专用），无通用 turn 级 load harness、无 MetricRegistry/export。

缺口：指标注册与导出、可重复 turn 级负载工具、统一 per-turn 追踪表面。约束：M5-M7 在独立 branch/worktree 执行；文档中文；commit 无 Co-Authored-By、无 emoji；不引入不必要的第三方依赖（清华 PyPI 镜像，依赖面谨慎）。

## Goals / Non-Goals

**Goals:**
- 提供可重复、机器可读、可复现的 turn 级负载工具，直接服务 C1B 导入基准与 C1D 并发验收。
- 进程内指标注册与 JSON/Prometheus 文本双格式导出，dashboard 可消费。
- 在既有诊断/事件基础上提供统一 per-turn 追踪表面，证明 S2 cutover 后（sqlite→postgres）可观测性不断。
- 负载结果与指标证据入 `openspec/evidence/c0/`，可复现、可对账。

**Non-Goals:**
- 不做 C2/C3 才有 hop（ingress queue、transactional outbox、delivery DLQ）的端到端 turn_id 追踪。
- 不引入 OpenTelemetry / Prometheus 服务端 / Grafana / 时序存储。
- 不做 5000 并发生产验收与真实 1024 维 recall 基线（C1D）。
- 不改 C1B/C1C 迁移工具、不改存储写路径；只读复用 seam。
- 不重构 dashboard 渲染层，只加指标数据源。

## Decisions

### D1 指标形态：进程内 MetricRegistry + 双格式 export，不引第三方依赖

**选择**：新增 `core/telemetry/metrics.py`：Metric 自描述（name/type/help/labels），支持 counter/histogram/timer；更新用单一锁（asyncio 单事件循环 + `threading.RLock` 兜底线程写）。Export 双格式：JSON（dict 序列化）与 Prometheus 文本（手写 exposition：`# HELP`/`# TYPE` + 样本行）。挂载到现有 dashboard/服务进程 HTTP 端点（新增 `/metrics` 路由）。

**备选**：prometheus-client 库 → 拒绝：新增第三方依赖 + 依赖面谨慎；指标量小，手写 exposition 足够；Metric 自描述对象保留后续换 prometheus-client 的 seam。
**备选**：仅 JSON → 拒绝：Prometheus 文本是 C1D 容量验收与后续抓取的标准形态，手写成本低。

### D2 负载工具形态：`scripts/load/` 独立 harness，复用 StorageRuntime seam

**选择**：`scripts/load/harness.py` 核心 + `scenarios/` 场景脚本 + CLI。驱动 turn 级负载：对每个 (tenant, 通道) 构造 InboundMessage → 走现有 passive turn 路径 → 统计成功率/延迟分位。后端经 config `storage.backend` 切换（与 `test_storage_parity` 同口径）。结果 JSON（输入参数 + 每 backend 轮次/成功率/延迟分位）写 `openspec/evidence/c0/results/`。**两种 reasoner 模式**：(a) 真实 LLM 冒烟（小批量）；(b) 注入式脚本 reasoner 做高并发（隔离存储/管道吞吐与 LLM 成本，避免 5000 并发烧 LLM 配额）。

**备选**：封装 `provision_5000_bench` → 拒绝：那是 provisioning DDL 专用 bench，本 change 需要 turn 级全链路负载，语义不同。
**备选**：负载直接驱动真实 LLM 全量 → 拒绝：并发验收的成本不可控；脚本 reasoner 是标准压测隔离手段。

### D3 turn_id 追踪表面：基于既有 diagnostic_log/lifecycle event 做统一收集，不重建日志栈

**选择**：新增轻量 `TraceStore`（进程内，按 turn_id 聚合各 phase 的 start/end/耗时/结果；可落盘 JSON 为证据）。收集点复用 `PassiveTurnPipeline` 现有 phase 边界（`agent/core/passive_turn.py:377-596`）与 `bus/events_lifecycle.py` 的 `turn_id` 字段。**不引入全链路追踪框架**（无 OpenTelemetry）。

**备选**：OpenTelemetry → 拒绝：外部 exporter/agent 依赖与运行成本远超本阶段收益；现有结构化日志 + lifecycle turn_id 已覆盖当前 hop。

边界（spec 已声明）：ingress queue/outbox/delivery 等 C2/C3 hop 不在本 change 追踪；S2 cutover 验证 = 同一 turn 在 sqlite→postgres 下追踪记录均完整。

### D4 dashboard 消费：经 `/metrics` 端点读取，不直接耦合 Registry

**选择**：dashboard 进程挂载 `/metrics`（D1 export），现有 metric tile 组件（ChartTone/MetricTile/TrendChart）加一个指标数据源读取 export 数据；保留现有 SQLite 读路径不动。

**备选**：dashboard 直接读 Registry/TraceStore → 拒绝：同进程耦合渲染与指标生命周期；经 export 端点与真实消费者（Prometheus 抓取）同路径，测试口径一致。

### D5 证据与可复现性

负载结果与指标导出写 `openspec/evidence/c0/`（复用 phase1b evidence 目录模式），JSON 含输入参数，重跑口径一致。C1B 通过调用本 harness 产出导入基准证据（任务级引用，不耦合代码）。

## Risks / Trade-offs

- [负载 harness 驱动真实 LLM 成本不可控] → 高并发用脚本 reasoner，真实 LLM 仅小批量冒烟；harness 显式两种模式。
- [指标导出在 asyncio + 线程写混合下竞态] → Metric 更新单一锁；导出时快照，不持有锁跨 I/O。
- [TraceStore 随 turn 量内存膨胀] → 进程内环形缓冲/上限，落盘按需；dashboard 读证据文件而非实时全量。
- [手写 Prometheus exposition 细节出错] → 标准示例断言测试覆盖格式；数据量小，风险可控。
- [C0 与 C1B 并行 worktree 文档冲突] → C0 不改迁移工具与存储写路径，冲突面仅 openspec 文档，合并时按文档规则处理。

## Migration Plan

- 独立 worktree/branch（`feature/c0-observability`），与 `feature/phase1b-migration` 并行。
- 落地顺序：harness + MetricRegistry（无侵入新增）→ 接入 dashboard `/metrics` 与 turn 追踪收集点 → 产出 C1B 引用基准证据。
- 回滚：全部为新增模块与新增端点，删除即回滚；不触碰生产写路径与既有 dashboard 读路径。

## Open Questions

- C1D 前是否需要从手写 exposition 切换到 prometheus-client 抓取 —— 由 C1D 容量验收形态决定，不影响本 change 的 spec/任务拆分。
- 5000 并发场景的 tenant/通道分布参数由 C1D 数据分布定案后校准 —— 不影响本 change 架构与任务拆分。
