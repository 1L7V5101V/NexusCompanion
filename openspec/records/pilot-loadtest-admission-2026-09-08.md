# Pilot admission/overload 压测基准（C3 落地验证）

> 归属：Pilot 冻结容量与恢复语义验证（[PILOT_ROADMAP §5.9.4/§5.9.5/§5.9.6](../PILOT_ROADMAP.md)），harness 覆盖 C3 change `c3-admission-queue-recovery` 已合入代码
> 分支：`main`（本地 dev worktree）
> 运行日期：2026-09-08（git `b05692b6`，结果 JSON `run_at=2026-09-08T15:25:17Z`）
> 结果原始数据：[`openspec/evidence/c3-admission-queue-recovery/results/pilot_loadtest-2026-09-08.json`](../../evidence/c3-admission-queue-recovery/results/pilot_loadtest-2026-09-08.json)
> harness：`tests/pilot_loadtest_bench.py`（独立脚本，pytest 不收集 `pilot_*_bench.py`）

## 1. 为什么要做（依据）

PILOT_ROADMAP §5.9.5「Admission 与 overload policy」冻结了系统容量边界：interactive 全局入站 128 / per-tenant pending 16 / maintenance 64 / WS 出站 hard 256（soft 192）/ LLM/embedding/MCP/process 资源信号量 30/4/8/2；§5.9.6「durable control plane 与 restart recovery」要求非终态 turn 在重启后全部得到可解释处置。这些语义随 C3 合入（PR #1），但此前只有单元/集成测试验证**行为是否正确**，没有用可控负载把**容量边界、降级曲线、隔离性、恢复吞吐**一次性量化。本基准要回答：

1. **拒绝曲线**：全局有界队列 128 与 per-tenant 16 是否真的「到点即拒、不无限堆积、已接受项不丢」——第几条开始拒、拒绝回执长什么样、FIFO 是否保序。
2. **慢消费者降级**：WS 消费者追不上时，soft/hard 两档是否按冻结阈值生效，重放机制能否保证 terminal 帧零丢失补回。
3. **隔离与优先级**：多租户并发是否互相不阻塞（墙钟不随租户数增长）、interactive 是否压过 maintenance、全局资源信号量是否真的封顶并发。
4. **重启恢复**：StartupRecoveryScanner 对大量非终态 turn 的处置动作分布、耗时与残留——为 P3 恢复演练提供基线工具。

## 2. 怎么做的（方法与实现）

### 2.1 范围与诚实边界

- **全部驱动生产类**（不复制降级/拒绝逻辑）：`MessageBus`、`PassiveMessageWorker`、`TenantLaneRouter`、`ResourceSemaphores`、`WebChatChannel._Connection/_broadcast` + 真实重放 buffer、`StartupRecoveryScanner` + `TurnAuditRecoverySource` + 真实 SQLite `SessionStore`（control store 语义验证用；生产为 PG，见 §3.2 局限）。
- **LLM/turn 用可控延时 stub**（mock LLM）：`_FakeRuntime` 只实现 `wait_thread_available/start_turn/handle.result()` 三个方法，每轮固定 `delay_s`（默认 5ms）。这隔离上游变量、只测自系统容量边界；**结果不代表生产 LLM 端到端延迟**。
- **实现注意两处**：
  - Windows ProactorEventLoop 会合并 <~15ms 的 `asyncio.sleep`（busy 时近乎 0ms、idle 时按 15.6ms 量子唤醒），节奏性延时全部改用 `_delay_s()`（perf_counter 自旋），实测 5/10/20/50ms 在 busy/idle 下均精确命中。
  - s4 恢复扫描是同步 IO（逐条状态迁移落库），经 `asyncio.to_thread` 执行并在 `finally` 关闭 store，避免 Windows 下临时库被占用。

### 2.2 运行

```bash
python tests/pilot_loadtest_bench.py                                    # 默认全量 → results/pilot_loadtest.json
python tests/pilot_loadtest_bench.py --only s2 --deltas 2000 --terminals 1500   # 单场景调参
python tests/pilot_loadtest_bench.py --out openspec/evidence/c3-admission-queue-recovery/results/pilot_loadtest-2026-09-08.json  # 归档用
```

默认参数：`--flood 5000 --burst 200 --deltas 400 --terminals 300 --tenants 8 --turns 25 --recovery-turns 2000`；`--timeout 60`（单场景超时）；JSON 含 meta（git rev / run_at / params / interpretation）与每场景 metrics + 自检断言（checks）。

### 2.3 各场景测法

| 场景 | 被测生产代码 | 指标测法 |
|---|---|---|
| S1a 全局入站拒绝曲线 | `MessageBus.publish_inbound`（有界 128） | 5000 条洪峰（无消费者）至满载：已接受条数、首拒序号、Retry-After、队列深度峰值；随后 drain 全部已接受项校验 FIFO/无重复无缺失；tracemalloc 记录洪峰内存增量 |
| S1b per-tenant 拒绝回执 | `PassiveMessageWorker` 真路径 + 慢 lane（5ms/turn） | 单租户突发 200（逐条发布并让出事件循环）：lane 深度峰值、被拒条数、`nexus_overload` 回执数；断言每条消息恰好收到 ok 或拒绝回执之一（无静默丢失/无重复） |
| S2 慢消费者 WS 分级降级 | `WebChatChannel._broadcast` + 真实 `_Connection`/重放 buffer（stub WebSocket 只记 close，零消费） | soft 段广播 400 条 delta：丢帧数、`replay_required` 通知次数；hard 段广播 300 条 terminal：封顶前入队数、close code、盖 seq 数；重连后 `frames_after` 全量/断点补拉覆盖、去重、seq 递增 |
| S3 tenant 隔离 / 优先级 / 资源 | `TenantLaneRouter` + `ResourceSemaphores` | 8 租户 × 25 轮 5ms work：per-tenant busy 区间重叠数、总墙钟 vs 单租户基线（ratio）；interactive 活跃期同租户/跨租户 maintenance 的延后与等待；LLM=30 下 60 并发 max active、等待 p50/p95 |
| S4 重启恢复扫描 | `StartupRecoveryScanner` + `TurnAuditRecoverySource` + SQLite `SessionStore` | 播种 2000 条非终态 turn（70% queued / 30% in_progress）→ 单次 scan：动作分布、apply 异常数、扫描耗时、扫描后残留 |

## 3. 结果

运行配置：默认参数（见 §2.2），完整数据以 JSON 为准，下表只取其中字段。

### S1a 全局入站拒绝曲线（`s1-global-overflow`）

| 指标 | 结果 |
|---|---|
| 队列容量 / 洪峰总量 | 128 / 5000 |
| 已接受 / 被拒 | **128 / 4872** |
| 首拒位置 / Retry-After | **第 129 条** / 5.0s（`AdmissionOverloadError`, `global_interactive`） |
| 洪峰墙钟 / 队列深度峰值 | 0.188s / 128 |
| FIFO 保序 / 静默丢失 | true / false |

### S1b per-tenant 拒绝回执（`s1b-per-tenant-rejection`）

| 指标 | 结果 |
|---|---|
| per-tenant pending 容量 | 16 |
| 突发 200 条：被接受 / 明确拒绝 | 18 / **182**（拒绝率 0.91） |
| lane 深度峰值（采样） | 16（= 容量，无越界） |
| `nexus_overload` 拒绝回执 | 182（1:1 对应每次拒绝） |
| settle 墙钟 / 单租户 ok 速率 | 0.078s / ≈231 turn/s |
| 无静默丢失 / 无重复乱序 | true / true |

### S2 慢消费者 WS 分级降级（`s2`）

| 指标 | 结果 |
|---|---|
| soft / hard 阈值 | 192 / 256 |
| delta 广播 400：soft 起丢 / 通知 | **208 条丢弃**，`replay_required` 仅 1 次 |
| terminal 广播 300：封顶前入队 / hard 丢 | 256 / 44（不静默丢，进重放 buffer） |
| hard 断开 | close code **1013**（overload） |
| 重放补拉：全量 / 断点（after_seq=256） | 300/300 全覆盖；补 44 条；seq 递增、无重复 |

### S3 tenant 隔离 / 优先级 / 资源（`s3`）

| 指标 | 结果 |
|---|---|
| 单租户基线 / 8 租户 × 25 轮 | 125ms / 125ms（**ratio 1.00**） |
| 同租户 busy 区间重叠 | 0 |
| maintenance：同租户延后 / 跨租户等待 | Deferred×1 / p50 **0.0ms**（不被其他租户 interactive 阻塞） |
| LLM 信号量=30：60 并发 max active / 等待 | **30** / 30 个等待，等待 p50=p95≈31ms |

### S4 重启恢复扫描（`s4`）

| 指标 | 结果 |
|---|---|
| 播种非终态 turn | 2000（queued 1400 + in_progress 600） |
| 处置动作分布 | `cancelled` 2000（100%） |
| apply 异常 / 扫描后残留 | 0 / 0 |
| 扫描耗时 | 9.72s（逐条 SQLite 状态迁移，≈4.86ms/条） |

### 3.1 解读

- **契约数字与 LLM 无关，可以现在就写结论**：全局 128 / per-tenant 16 / WS 192-256 阈值全部按冻结值生效；第 129 条、第 17 条起即时拒绝且不静默丢；拒绝有明确回执（`nexus_overload` 出站文案），被接受消息 FIFO 保序恰好回一次；重放 buffer 保证 hard 断开后 terminal 帧 100% 无重复补回。同租户串行零重叠、跨租户墙钟 ratio 1.00 是 asyncio 协作式的结构性保证，不随单轮耗时变化。
- **性能数字随单轮耗时 t 线性缩放**：单租户吞吐 ≈ 1/t，semaphore 排队等待 p50/p95 ≈ 持有时长（此处 30ms 持有时 → 等待 p50/p95 31ms，说明无饿死、队列一轮收敛）。真实场景 t 由 LLM 时延主导：普通问答一轮 1~8s、thinking/多步工具 10s~分钟级，吞吐随之下降 3~4 个数量级；真实 p95 尾巴应接真 LLM 重测（当前 mock 为固定 5ms，无真实长尾）。
- **s1b 的 accepted 数量在 [17, ~100+] 间波动是预期的**：取决于 worker 消费与发布竞态（越慢的 turn、越大的突发窗口 → 更多在途被拒）。契约点（深度 ≤16、拒绝回执 1:1、无丢失）在多次运行中稳定；以本 JSON 的 accepted=18 / rejected=182 为一次记录值，不要当作确定容量。
- **恢复扫描 2000 条 9.7s 是 SQLite 逐条事务的成本**，非扫描逻辑本身（每记录一次 `transition_turn` commit ≈4.9ms）。生产 PG control store 下该成本形态不同，需在 PG 上复测；扫描的**语义结论**（非终态全 cancelled、0 异常、0 残留、动作全可解释）与存储无关。

### 3.2 局限

- mock turn（固定 5ms）无真实 LLM 长尾；所有吞吐/等待数字是「给定服务时延下的自系统容量边界」，不是生产端到端延迟。生产数字需接真 `ConversationRuntime` 重跑同场景（harness 的 `_FakeRuntime` 是唯一替换接缝）。
- 单进程 dev 环境（Windows，asyncio Proactor）；s4 用 SQLite `SessionStore` 代替 PG control store（P0.5 前 SQLite 仍是 control plane 语义等价载体，PG durable 落地后须复测）。
- 消费端为确定性假件（S2 零消费、S3 等长 work），未覆盖真实网络抖动/真实用户到达分布。
- s1b accepted 数存在运行间波动（见 §3.1），JSON 中每次运行都记录了当时的实际值。

## 4. 复现与校验

- 复现：重跑 §2.2 命令即可（约 25s；s4 2000 条播种+扫描占大头）。
- 数据完整性：结果 JSON 是唯一原始数据源；本文档表格只取其中字段，核对以 JSON 为准。
- 与路线图的一致性：S1a/S1b 对齐 §5.9.5 的 interactive 128 / per-tenant 16 与拒绝回执语义；S2 对齐 §5.9.4 slow consumer 阈值与 replay；S3 对齐 §5.9.5 的资源信号量 30/4/8/2 与 interactive>maintenance 优先级；S4 对齐 §5.9.6 重启恢复扫描（cancelled 语义、未知结果工具须确认后方可重放），并为 P3「恢复演练」条目提供可复用压测工具。
