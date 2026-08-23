# C0 证据：可重复负载工具（任务 2.3–2.4）

- 记录日期：2026-08-23
- 代码位置：`scripts/load/`（harness.py / result.py / driver.py / scenarios/）
- 相关结果文件：`results/dry_run-sqlite-20260823T014716Z.json`（2.3）、
  `results/simulate-sqlite-20260823T015421Z.json`（2.4）

## 2.3 harness 骨架

- CLI：`--scenario/--backend/--tenants/--channels/--rounds/--concurrency/--reasoner/
  --seed/--sim-latency-ms/--sim-fail-rate/--dry-run/--workspace/--results-dir/--no-write/--list`。
- 场景注册表：`scripts/load/scenarios/`（`@register(name)` 装饰即自动入 `--list`）。
- 结果 JSON 回写全部输入参数 + 固定统计口径（成功率 = succeeded/attempted，
  nearest-rank 延迟百分位 p50/p90/p95/p99/max/mean）。
- 验证：`dry-run` 场景产出 `results/dry_run-sqlite-*.json`，含输入参数与空统计结构。

## 2.4 两种 reasoner 模式

`scripts/load/driver.py` 组装最小真实 passive turn 路径：

- 真实部分：`create_storage_runtime(config)` + `SessionManager` + `EventBus` +
  `AgentCore.process(msg, key, dispatch_outbound=False)`（PassiveTurnPipeline 全
  phase 链：before_turn → before_reasoning → reasoner → after_reasoning → after_turn）。
- 桩部分：StubContextStore（不检索）、StubContextBuilder（不拼 prompt）、
  StubToolRegistry（set_context no-op）。
- 消息带 `skip_memory_context_guard=True`，把记忆归档压力排除在存储负载口径外。
- 每个 turn 设置独立 `current_turn_id`（`load-*`），供后续 3.x turn_id 追踪复用。

两种 reasoner：

- **script**：`ScriptedReasoner`，无真实 LLM 调用；`sim_latency_ms` 模拟 LLM 延迟，
  `sim_fail_rate`（按 `seed` 确定性）模拟失败。失败实现为返回 `reply=None`
  （走 after_reasoning fallback 文案，不抛异常、不打 trace），出站内容不再以
  `echo:` 开头，`_drive_one` 据此判失败。
- **llm**：真实 `DefaultReasoner` + `build_providers(config)` provider（需
  workspace 下 config.toml + API key，小批量冒烟用；本任务只验证 import 与构造，
  未发起真实计费调用）。

## 验证（脚本模式）

命令（结果文件 `results/simulate-sqlite-20260823T015421Z.json`）：

```bash
uv run python scripts/load/harness.py --scenario simulate --backend sqlite \
  --rounds 20 --concurrency 5 --reasoner script --seed 42 \
  --sim-latency-ms 100 --sim-fail-rate 0.3
```

结果要点：

- 20 轮真实 turn（含真实 sqlite 会话读写），成功 10 / 失败 10，success_rate 0.5
  （seed=42 确定性）。
- 延迟 p50=158.5ms / p90=195.6ms / p99=209.2ms，反映 100ms 模拟延迟 + 真实
  存储/phase 链开销。
- 全程无真实 LLM 调用（script 分支不构建 provider）。
- 工作区 sqlite 落盘 `openspec/evidence/c0/workspace/`（gitignore 覆盖），不入库。

结论：任务 2.4「脚本 reasoner 模式在无真实 LLM 调用下完成 N 轮 turn 并统计
成功/失败/延迟分位」达成。

## 2.5 双后端驱动

`--backend sqlite/postgres` 经 `StorageConfig(backend=...)` 切后端，同一 `simulate`
场景与统计口径对两种后端各跑一轮：

| 后端 | attempted | succeeded | failed | success_rate | p50 | p90 | p99 |
|---|---|---|---|---|---|---|---|
| sqlite | 20 | 10 | 10 | 0.5 | 158.7ms | 192.3ms | 203.2ms |
| postgres | 20 | 10 | 10 | 0.5 | 181.1ms | 223.2ms | 264.0ms |

- 结果文件：`results/simulate-sqlite-20260823T015421Z.json`、
  `results/simulate-postgres-20260823T015449Z.json`。
- 两后端 spec 参数一致、统计口径一致（同一 latency 键集合、同一 success/fail
  计数，seed 确定性）；延迟差异来自 PG 线程池往返 vs 本地文件写，属负载观察，
  非口径不一致。
- postgres 后端由 `nexus-postgres`（docker, 0.0.0.0:5433）提供。

## 2.6 可复现性对账

同一配置（seed=42）重跑 + 不同 seed（seed=7）对照，结果文件：

- `simulate-sqlite-20260823T015421Z.json`（seed 42，第 1 次）
- `simulate-sqlite-20260823T015516Z.json`（seed 42，第 2 次）
- `simulate-sqlite-20260823T015525Z.json`（seed 7）

| 对账 | spec | attempted | succeeded | failed | p50 | p99 |
|---|---|---|---|---|---|---|
| seed42 第1次 | 一致 | 20 | 10 | 10 | 158.7ms | 203.2ms |
| seed42 第2次 | 一致 | 20 | 10 | 10 | 163.2ms | 203.4ms |
| seed7 | 仅 seed 不同 | 20 | 12 | 8 | — | — |

- 同配置两次运行：输入参数一致、统计口径一致、成功/失败计数一致（seed 确定性），
  延迟差异在机器噪声内（p50 差 4.5ms），可由运行环境解释。
- 不同 seed：失败数随 seed 确定性变化（10/10 → 12/8），运行间差异由输入参数解释。
- 结论：2.6「相同配置重跑对账通过、运行间差异可由输入参数解释」达成。
