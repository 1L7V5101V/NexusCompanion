# C0 可观测性与负载工具 — 实施任务

> 执行环境：独立 branch/worktree（`feature/c0-observability`），与 `feature/phase1b-migration` 并行；不改迁移工具与存储写路径，只读复用 seam。
> 每任务含验证方式；管理闭环：任务 checkbox → `openspec status` → evidence → SCALING_ROADMAP/CHECKLIST 仅在有 evidence 时更新。

## 1. 基线与环境

- [ ] 1.1 创建独立 worktree/branch（`feature/c0-observability`），记录 main 与分支的 pytest/pyright 基线到 `openspec/evidence/c0/baselines/`。验证：`git status --short --branch` 干净、分支正确；基线文件存在且含 main 全量 1031 passed / 0 failed（2026-08-23 清理后）记录
- [ ] 1.2 确认可复用面：`diagnostic_log` phase 边界与 turn_id（`agent/core/passive_turn.py`）、`bus/events_lifecycle.py` turn_id 字段、dashboard 挂载点、config `storage.backend` 切换。验证：把各引用文件与行号记录到 evidence，作为后续接入点基线

## 2. 可重复负载工具（C0）

- [ ] 2.1 实现 `core/telemetry/` MetricRegistry（counter/histogram/timer、自描述 name/type/help、单一锁）并单测。验证：`pytest tests/test_metrics_registry.py` 覆盖类型注册、更新、快照线程安全
- [ ] 2.2 实现 JSON + Prometheus 文本 export（手写 exposition）并格式断言测试。验证：`pytest tests/test_metrics_export.py` 断言 JSON 字段完整、Prometheus 文本可被标准解析器读取
- [ ] 2.3 实现 `scripts/load/` harness 骨架（CLI 参数、场景注册、结果 JSON 写出 `openspec/evidence/c0/results/`）。验证：dry-run 场景产出含输入参数的 JSON 结果文件
- [ ] 2.4 实现两种 reasoner 模式（真实 LLM 小批量冒烟 + 注入式脚本 reasoner 高并发）。验证：脚本 reasoner 模式在无真实 LLM 调用下完成 N 轮 turn 并统计成功/失败/延迟分位
- [ ] 2.5 双后端驱动（经 config `storage.backend` 切 sqlite/postgres）。验证：对两种后端各跑一轮，结果文件含每后端轮次/成功率/延迟分位，两后端统计口径一致
- [ ] 2.6 可复现性验证：相同配置重跑，结果文件输入参数与统计口径一致、运行间差异可由输入参数解释。验证：两次运行结果对账通过；重跑证据入库

## 3. turn_id 追踪表面（C0）

- [ ] 3.1 实现 TraceStore（进程内按 turn_id 聚合 phase start/end/耗时/结果；环形缓冲上限 + 可落盘 JSON）。验证：`pytest tests/test_trace_store.py` 覆盖聚合、上限、落盘
- [ ] 3.2 收集点接入 `PassiveTurnPipeline` 现有 phase 边界与 lifecycle 事件 turn_id。验证：单 turn 后 TraceStore 存在按 turn_id 关联的完整 phase 记录
- [ ] 3.3 双后端一致性：sqlite→postgres 切换后运行 turn，追踪记录均完整。验证：`pytest tests/test_trace_backend_parity.py` 两后端下 turn_id 可跨阶段关联（spec 场景「后端切换后追踪连续」）

## 4. dashboard 消费

- [ ] 4.1 dashboard 进程挂载 `/metrics`（export 端点）。验证：请求 `/metrics` 返回 200 与可解析的 JSON/Prometheus 文本
- [ ] 4.2 dashboard metric tile 数据源读取 export 并展示 turn/存储/迁移指标。验证：dashboard 页面呈现指标数据，现有 SQLite 读路径（历史日志/交付记录）无回归

## 5. 收口与证据

- [ ] 5.1 全量验证：pyright（project + tests 两配置）与 pytest 相对 main 基线无回归。验证：命令结果 + 与 `openspec/evidence/c0/baselines/` 记录的 main 基线 diff，无本分支新增失败
- [ ] 5.2 更新 change 状态与跟踪文档：tasks 全勾选 → `openspec validate` 通过 → `openspec status` 显示 apply 完成 → sync-specs 把 `observability-load` 增量合入主 spec → archive change。验证：`openspec validate` 通过、change archived
- [ ] 5.3 更新 `SCALING_ROADMAP.md` 与 `PROJECT_CHECKLIST.md`：仅在 evidence 齐全（merge commit + 可复现测试/基准）时更新 C0 状态；phase 归属沿用 planning 阶段已定案的前置位置。验证：两文档状态与 evidence 一致、`verified` 满足 §8 规则
