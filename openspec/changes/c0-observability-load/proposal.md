# C0 可观测性与负载工具（可重复负载 + 指标导出 + turn_id 追踪分段）

## Why

5000 用户扩展（SCALING_ROADMAP 目标）需要可重复的负载/基准工具与指标导出：C1B（M5）导入吞吐的证据、C1D（M7）真实 1024 维 embedding / 5000 并发验收都依赖可复现的压测与观测。当前现状：只有 provisioning 专用 bench（`tests/provision_5000_bench.py`）与日志式 turn 诊断（`core/common/diagnostic_log.py` 的 `diagnostic_line`/`diagnostic_context`），没有通用负载 harness、没有指标注册/导出，dashboard（`bootstrap/dashboard_api.py`）只读 SQLite workspace DB 无实时指标。C0 前置到 Phase 1B 之前，C1D 才不会被阻塞，C1B 的「1 万行导入耗时显著低于逐行基线」证据才可复现、可审计。

## What Changes

- **可重复负载/基准工具**：通用 turn 级 load harness（新增 `scripts/load/`），支持可配置 tenant/通道/注入速率/目标 backend（sqlite/postgres，走现有 `StorageRuntime` seam），输出机器可读 JSON 结果并入库 `openspec/evidence/c0/`；为 C1B 导入基准与 C1D 并发验收提供同一套可复现工具。
- **指标导出**：轻量进程内 MetricRegistry（counter/histogram/秒表）+ JSON / Prometheus 文本格式 export 端点；dashboard 消费同一指标源；不强制新增第三方依赖（Prometheus 格式可手写 exposition 文本），保留后续对接 exporter 的 seam。
- **turn_id 统一追踪表面（分段落地）**：在现有 `diagnostic_line`/lifecycle event 基础上提供 per-turn 追踪表面（turn_id → 各 phase 时间与结果），覆盖当前已存在的 hop（ingress → before_turn → before_reasoning → reasoner → after_reasoning → after_turn → store），用于 S2 cutover 后验证可观测性未断；ingress queue / transactional outbox / delivery 等 C2/C3 才有 hop 的端到端追踪不在本 change 范围。
- **dashboard 扩展**：指标数据源接入现有 dashboard 展示 turn / 存储 / 迁移指标。

## Non-Goals

- 不做 C2/C3 才有的 hop（ingress queue、transactional outbox、delivery DLQ）之上的端到端 turn_id 追踪。
- 不做 5000 并发生产验收与真实 1024 维 recall 基线（属 C1D）。
- 不做 Redis cache / read replica（属 C6）。
- 不改 C1B/C1C 迁移工具本身（本 change 只提供其基准所需的可复现工具，不把负载/可观测逻辑耦合进迁移代码）。
- 不引入 Prometheus 服务端 / Grafana / 外部指标栈（阶段内只做注册 + 导出 + dashboard 消费）。

## Capabilities

### New Capabilities
- `observability-load`: 可重复负载/基准工具、指标注册与导出、turn_id 统一追踪表面（分段到当前已存在的 hop）。

### Modified Capabilities
- 无（`scaling-governance` 无 requirement 变更；C0 是新增能力，不改既有治理规则）。

## Impact

- **代码**：新增 `scripts/load/`（harness + 场景脚本）、`core/telemetry/`（MetricRegistry + exporter）；dashboard 增加指标读取端；复用现有 `diagnostic_log` 与 `StorageRuntime` seam，不改迁移工具。
- **配置**：指标/负载相关配置新增到 config（enable、export interval、场景参数）。
- **依赖**：无新增第三方依赖预期（指标 stdlib + 现有 dashboard 栈）。
- **系统/运维**：export 端点挂载到现有 dashboard/服务进程；负载结果与指标证据入 `openspec/evidence/c0/`。
- **测试**：load harness 正确性/幂等测试、MetricRegistry 单测、turn 追踪表面测试。
