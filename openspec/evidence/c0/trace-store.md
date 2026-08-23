# C0 证据：turn_id 追踪表面（任务 3.1–3.3）

- 记录日期：2026-08-23
- 代码位置：`core/telemetry/trace_store.py`、`agent/core/passive_turn.py`（收集点）
- 测试：`tests/test_trace_store.py`（3.1）、`tests/test_trace_backend_parity.py`（3.3）

## 3.1 TraceStore

`core/telemetry/trace_store.py`：

- `TraceSpan`：turn_id / phase / started_at / duration_ms / status(ok|fail) / detail。
- `TraceStore`：进程内环形缓冲（`deque(maxlen=4096)`，防无限膨胀），单锁保护；
  `spans_for(turn_id)` / `phases_for(turn_id)` 按 turn_id 聚合；`dump_json`
  落盘全部 span 到 JSON。
- `trace_phase` 上下文管理器：进入记 begin，正常退出记 ok，异常退出记 fail（不吞异常）；
  store 为 None 时 no-op（生产默认零开销）。

验证：`pytest tests/test_trace_store.py` — 7 passed（聚合、上限淘汰最旧、失败标
fail、落盘 round-trip、显式路径、max_entries 校验）。

## 3.2 收集点接入 PassiveTurnPipeline

`agent/core/passive_turn.py`：

- `AgentCoreDeps` 新增 `trace_store: TraceStore | None = None`（默认 None，不改变
  现有调用方）。
- `PassiveTurnPipeline.run` 五个 phase 边界各包一层 `trace_phase(trace_store,
  turn_id, "<phase>")`：before_turn / before_reasoning / reasoner /
  after_reasoning / after_turn。turn_id 用 pipeline 既有 `_turn_log_id(key, msg)`
  （与 diagnostic 日志同 id）。

验证：单 turn 走真实 pipeline（script reasoner），TraceStore 存在按一个 turn_id
聚合的完整 5 段 phase 记录，顺序 = 上述五阶段，status 全 ok：

```
turn 49eec4c5: phases = [before_turn(1.1ms), before_reasoning(0.2ms),
  reasoner(0.0ms), after_reasoning(22.2ms), after_turn(0.3ms)]
```

回归：既有 pipeline/phase 测试（test_turn_pipelines、test_lifecycle_phases、
test_agent_core_p5/p6/p7）52 passed，无回归。

## 3.3 双后端一致性

`tests/test_trace_backend_parity.py`：同一份断言参数化跑 `sqlite` 与 `postgres`
（PG 不可用时自动 skip，`NEXUS_TEST_PG_URL` 可覆盖）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_trace_backend_parity.py -v
# test_trace_complete_and_correlated[sqlite]   PASSED
# test_trace_complete_and_correlated[postgres] PASSED
```

两后端下单 turn 均产出完整且跨 phase 关联的 TraceSpan：phase 序列 =
[before_turn, before_reasoning, reasoner, after_reasoning, after_turn]，同一
turn_id 聚合，status 全 ok（spec 场景「后端切换后追踪连续」）。测试环境用
`docker/debug/docker-compose.yml` 的 `postgres` 服务（pgvector:pg17，宿主 5433）。
