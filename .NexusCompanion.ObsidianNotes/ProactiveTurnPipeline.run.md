[[主动链路]]的“[[多层门控]]”主要在 [agent/core/proactive_turn.py](<D:\.Projects\NexusCompanion\agent\core\proactive_turn.py>) 里实现，核心入口是 `ProactiveTurnPipeline.run()`。
```text

ProactiveTurnPipeline — 主动回复链路顶层抽象。

设计对齐被动链路的 PassiveTurnPipeline.run()：

通过 run() 一个方法可见全链路。

  
┌─ tick trigger

│  └─ ProactiveTurnPipeline.run()

│     ├─ 1. Gate      准入检查（busy / cooldown / anyaction / fallback）

│     ├─ 2. Fetch     拉取数据（alerts / content / context → messages）

│     ├─ 3. Judge     LLM 评估（多轮工具调用：分类 → 草稿 → 收尾）

│     ├─ 4. Resolve   决策去重（skip/reply + delivery_dedupe + message_dedupe）

│     └─ 5. Deliver   执行发送（dispatch + ACK + persist + tick_log）

└─ done
 

段之间通过 AgentTickContext 传递状态，每段各司其职，不跨段直接访问对方内部实现。

后续可按需将任一段升级为 Phase 模块链，对外接口不变。

```

**你可以在面试里这样说**：

> 主动链路的门控分为前置准入和后置裁决两层。前置 `_gate_check` 判断目标是否存在、被动链路是否 busy、是否处于 delivery cooldown、AnyActionGate 是否允许本轮行动；后置 `_resolve_decide` 根据 LLM 的 terminal_action、delivery_key 去重和语义去重决定最终 reply 还是 skip。只有所有 gate 通过后，才会进入 `_deliver_execute` 真正发送。

---
它的结构大概是：
```text
run()
 -> _gate_check()       # 前置门控：这一轮要不要开始
 -> _fetch_pull()       # 拉取数据
 -> _judge_evaluate()   # LLM 判断
 -> _resolve_decide()   # 后置门控：发不发、去重
 -> _deliver_execute()  # 真正发送
```

## **1. 前置门控**
代码在 [agent/core/proactive_turn.py](<D:\.Projects\NexusCompanion\agent\core\proactive_turn.py:340>)：

```python
gate = self._gate_check(ctx)
if gate.blocked:
    self._record_tick_log_finish(ctx, gate_exit=gate.reason)
    return gate.base_score
```

意思是：每次主动链路 tick 醒来后，先跑 `_gate_check()`。如果被挡住，就直接结束本轮，不拉数据、不调用 LLM、不发消息。

具体门控在 [agent/core/proactive_turn.py](<D:\.Projects\NexusCompanion\agent\core\proactive_turn.py:379>)：

```python
def _gate_check(self, ctx: AgentTickContext) -> GateResult:
```

里面有几层判断：

**没有目标用户，不发**

```python
if not str(self._cfg.default_chat_id or "").strip():
    return GateResult(blocked=True, reason="no_target", base_score=None)
```

意思是没有配置默认 `chat_id`，不知道发给谁，直接跳过。

**被动链路忙，不发**

```python
if self._passive_busy_fn and self._passive_busy_fn(self._session_key):
    return GateResult(blocked=True, reason="busy", base_score=None)
```

这个就是 `busy gate`。如果用户正在和 Agent 对话，主动链路不要插话。

`passive_busy_fn` 是在 [bootstrap/proactive.py](<D:\.Projects\NexusCompanion\bootstrap\proactive.py:80>) 注入的：

```python
passive_busy_fn=(
    agent_loop.processing_state.is_busy if agent_loop.processing_state else None
)
```

也就是说，主动链路会查看被动链路当前是不是正在处理消息。

**冷却时间内，不发**

```python
if self._state_store.count_deliveries_in_window(
    self._session_key,
    self._cfg.agent_tick_delivery_cooldown_hours,
) > 0:
    return GateResult(blocked=True, reason="cooldown", base_score=None)
```

这就是 `cooldown gate`。如果最近已经发过主动消息，就不再发，避免连续骚扰。

**AnyActionGate：配额 + 最小间隔 + 概率门**

```python
should_act, meta = self._any_action_gate.should_act(
    now_utc=ctx.now_utc,
    last_user_at=self._last_user_at_fn(),
)
if not should_act:
    return GateResult(blocked=True, reason="presence", base_score=None)
```

它实际实现在 [proactive_v2/anyaction.py](<D:\.Projects\NexusCompanion\proactive_v2\anyaction.py:114>)。

里面有三层：

```python
remaining = max(0, self._cfg.anyaction_daily_max_actions - snap.used)
if remaining <= 0:
    return False, {"reason": "quota_exhausted", ...}
```

每日次数用完，不行动。

```python
if since_last < self._cfg.anyaction_min_interval_seconds:
    return False, {"reason": "min_interval", ...}
```

距离上次行动太近，不行动。

```python
idle_factor = 1.0 - math.exp(-idle_min / ...)
p = probability_min + (...) * idle_factor * time_factor
draw = random()
return draw < p, {...}
```

根据用户空闲时间计算概率。用户越久没互动，行动概率越高；然后抽样决定这轮是否允许继续。

## **2. 后置门控**
前置门控通过后，系统会拉数据、让 LLM 判断。如果 LLM 决定不发，会走 skip。

代码在 [agent/core/proactive_turn.py](<D:\.Projects\NexusCompanion\agent\core\proactive_turn.py:602>)：

```python
if ctx.terminal_action != "reply":
    skip_result = TurnResult(
        decision="skip",
        outbound=None,
        ...
    )
    return ResolveResult(action="skip", result=skip_result)
```

意思是：如果 LLM 没有明确决定 `reply`，默认不发。

然后还有两个去重门控。

**delivery 去重**

```python
delivery_key = build_delivery_key(ctx)
if self._state_store.is_delivery_duplicate(
    self._session_key, delivery_key, self._cfg.delivery_dedupe_hours
):
    return ResolveResult(action="skip", ...)
```

这是基于来源/证据的去重。比如同一批 GitHub issue 或同一条内容，短时间内已经发过，就跳过。

**message 语义去重**

```python
is_dup, reason = await self._deduper.is_duplicate(
    new_message=ctx.final_message,
    recent_proactive=recent_proactive,
    new_state_summary_tag="none",
)
if is_dup:
    return ResolveResult(action="skip", ...)
```

这是基于消息语义的去重。即使来源不完全一样，但生成出来的话和最近主动消息太像，也跳过。

## **3. 通过门控后才发送**
如果上面都通过，才构造真正的发送结果：

```python
send_result = TurnResult(
    decision="reply",
    outbound=TurnOutbound(
        session_key=self._session_key,
        content=ctx.final_message,
    ),
    ...
)
```

也就是说，“门控”在代码里不是抽象概念，而是明确的 `if blocked -> return skip`。

