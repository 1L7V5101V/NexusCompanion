# Akasic Agent Passive Reply Layer

```text
当前仓库里的被动回复层
是怎么按“大块抽象 <-> 大块抽象”组织起来的
```

不讲 proactive。
不讲全项目营销式介绍。
只讲被动主链。

---

## 1. 目标

被动回复层现在追求的是这件事：

```text
┌──────────────────────────────────────┐
│ 大块抽象之间                         │
├──────────────────────────────────────┤
│ 1. 用稳定协议对接                    │
│ 2. 每块只管自己那一段职责            │
│ 3. 块内实现可以继续拆                │
│ 4. 收尾时不靠隐式状态传语义          │
└──────────────────────────────────────┘
```

换句话说，这一层现在不是“一个大函数把所有事做完”，而是：

```text
prepare -> execute -> commit
```

三段主流程，分别交给不同大块负责。

---

## 2. 当前主链

当前默认被动主链是：

```text
┌──────────────────────────────────────┐
│ AgentLoop                            │
│ runtime shell                        │
└──────────────────┬───────────────────┘
                   │
                   v
┌──────────────────────────────────────┐
│ CoreRunner.process()                 │
│ 分流：spawn completion / 普通消息    │
└──────────────────┬───────────────────┘
                   │
                   v
┌──────────────────────────────────────┐
│ AgentCore.process()                  │
│ 1. prepare                           │
│ 2. build prompt                      │
│ 3. reasoner.run_turn()               │
│ 4. commit                            │
└───────────────┬───────────────┬──────┘
                │               │
                v               v
┌────────────────────────┐  ┌────────────────────────┐
│ ContextStore           │  │ Reasoner               │
│ prepare / commit       │  │ run_turn / run         │
└────────────────────────┘  └────────────────────────┘
```

这条链里，大块职责已经比较清楚：

- `AgentLoop`：运行时入口壳
- `CoreRunner`：分流层
- `AgentCore`：主编排层
- `ContextStore`：上下文准备与提交边界
- `Reasoner`：执行层

---

## 3. 大块抽象

### 3.1 AgentLoop

角色：

```text
runtime shell
```

负责：

- `run()`
- `_process()`
- `process_direct()`
- `trigger_memory_consolidation()`
- 组装 passive runtime

不负责：

- retrieval 细节
- tool loop 细节
- commit 落盘细节

它的意义不是“业务中心”，而是：

```text
把 runtime 入口和被动主链装起来
```

---

### 3.2 CoreRunner

角色：

```text
主链分流层
```

当前职责很单纯：

- `spawn completion` 走 helper
- 普通被动消息走 `AgentCore`

接口形态：

```text
输入
- msg: InboundMessage
- key: str
- dispatch_outbound: bool

输出
- OutboundMessage
```

这层的价值是：

```text
外层 runtime 不需要知道
内部事件和普通消息
分别怎么跑
```

---

### 3.3 AgentCore

角色：

```text
主流程编排 facade
```

当前固定流程：

```text
┌──────────────────────────────────────┐
│ AgentCore.process()                  │
├──────────────────────────────────────┤
│ 1. session                           │
│ 2. context_store.prepare()           │
│ 3. build_system_prompt()             │
│ 4. tools.set_context()               │
│ 5. reasoner.run_turn()               │
│ 6. context_store.commit()            │
└──────────────────────────────────────┘
```

接口形态：

```text
输入
- msg: InboundMessage
- key: str
- dispatch_outbound: bool

输出
- OutboundMessage
```

它不应该知道：

- retrieval pipeline 怎么查
- tool loop 怎么多轮跑
- commit 内部怎么落盘/observe/dispatch

它只负责：

```text
把 prepare / execute / commit 串起来
```

---

### 3.4 ContextStore

角色：

```text
上下文边界 + 提交边界
```

它被故意拆成两个固定大接口。

#### `prepare()`

接口：

```text
输入
- msg
- session_key
- session

输出
- ContextBundle
```

负责：

- 读 session history
- 调 retrieval pipeline
- 收 `skill_mentions`
- 生成 `ContextBundle`

`ContextBundle` 里现在承载的是被动链真正需要的正式字段，例如：

- `history`
- `skill_mentions`
- `retrieved_memory_block`
- `retrieval_trace_raw`

#### `commit()`

接口：

```text
输入
- msg
- session_key
- reply
- tools_used
- tool_chain
- thinking
- retrieval_raw
- context_retry
- post_turn_actions
- dispatch_outbound

输出
- OutboundMessage
```

负责：

- session append
- observe trace
- post_turn schedule
- `post_turn_actions`
- meme decorate
- outbound dispatch

也就是说：

```text
prepare 管“本轮怎么准备输入”
commit  管“本轮怎么提交结果”
```

---

### 3.5 Reasoner

角色：

```text
执行层
```

现在是双层结构，但两层职责已经不同。

#### `run_turn()`

角色：

```text
完整被动执行入口
```

接口：

```text
输入
- msg
- session
- skill_names
- base_history
- retrieved_memory_block

输出
- TurnRunResult
```

负责：

- build retry plan
- trim history / sections
- build preflight
- 调 `run()`
- 生成 `TurnRunResult`

`TurnRunResult` 目前包含：

- `reply`
- `tools_used`
- `tool_chain`
- `thinking`
- `context_retry`

#### `run()`

角色：

```text
单次 tool loop 原语
```

接口：

```text
输入
- initial_messages
- request_time
- preloaded_tools
- preflight_injected

输出
- ReasonerResult
```

负责：

- 调模型
- 执行工具
- tool_search 解锁
- reflect
- repeat guard
- incomplete summary fallback

这两层现在不要混淆：

```text
run_turn()
├─ 完整被动执行
└─ 包括 retry / trim / preflight

run()
├─ 低层 tool loop
└─ 是执行原语
```

---

## 4. 入参 / 出参协议

### 4.1 CoreRunner

```text
CoreRunner.process(
  msg,
  key,
  dispatch_outbound=True,
) -> OutboundMessage
```

### 4.2 ContextStore

```text
ContextStore.prepare(
  msg,
  session_key,
  session,
) -> ContextBundle
```

```text
ContextStore.commit(
  msg,
  session_key,
  reply,
  tools_used,
  tool_chain,
  thinking,
  retrieval_raw,
  context_retry,
  post_turn_actions=None,
  dispatch_outbound=True,
) -> OutboundMessage
```

### 4.3 Reasoner

```text
Reasoner.run_turn(
  msg,
  session,
  skill_names=None,
  base_history=None,
  retrieved_memory_block="",
) -> TurnRunResult
```

```text
Reasoner.run(
  initial_messages,
  request_time=None,
  preloaded_tools=None,
  preflight_injected=False,
) -> ReasonerResult
```

### 4.4 AgentCore

```text
AgentCore.process(
  msg,
  key,
  dispatch_outbound=True,
) -> OutboundMessage
```

---

## 5. 块内怎么做功能划分

### 5.1 ContextStore 内部分工

```text
prepare
├─ history -> retrieval 输入格式
├─ retrieval request
├─ skill mention 收集
└─ ContextBundle

commit
├─ session append
├─ runtime metadata update
├─ observe
├─ post_turn
├─ post_turn_actions
├─ meme decorate
└─ outbound dispatch
```

### 5.2 Reasoner 内部分工

```text
run_turn
├─ retry trace
├─ trim plan
├─ preflight
├─ assembled input
├─ 调 run
└─ TurnRunResult

run
├─ tool visibility
├─ llm call
├─ tool execution
├─ procedure hint / intercept
├─ tool_search unlock
├─ repeat guard
└─ summary fallback
```

### 5.3 AgentCore 内部分工

```text
AgentCore
├─ 读取 session
├─ 取 ContextBundle
├─ 渲染 prompt preview
├─ 设置 tool context
├─ 执行 run_turn
└─ 提交 commit
```

这里故意不让 `AgentCore` 直接吸收：

- retrieval 实现
- retry 策略
- tool loop 细节
- commit 内部副作用

否则它会重新膨胀成巨石。

---

## 6. 一次完整被动回复怎么跑

```text
┌──────────────┐
│ 用户消息进入 │
└──────┬───────┘
       v
┌──────────────────────┐
│ AgentLoop._process() │
└──────┬───────────────┘
       v
┌──────────────────────┐
│ CoreRunner.process() │
└──────┬───────────────┘
       v
┌──────────────────────┐
│ AgentCore.process()  │
├──────────────────────┤
│ 1. prepare           │
│ 2. prompt preview    │
│ 3. run_turn          │
│ 4. commit            │
└──────┬───────────────┘
       v
┌──────────────────────┐
│ OutboundMessage      │
└──────────────────────┘
```

再展开一点：

```text
AgentCore
├─ ContextStore.prepare()
│  ├─ history
│  ├─ retrieval
│  └─ ContextBundle
├─ Reasoner.run_turn()
│  ├─ retry / trim / preflight
│  └─ run()
│     ├─ tool loop
│     ├─ tool_search
│     └─ final reply
└─ ContextStore.commit()
   ├─ session
   ├─ observe
   ├─ post_turn
   ├─ meme
   └─ dispatch
```

---

## 7. 现在这套架构的意义

当前被动层已经基本不是“高耦合巨石”了。

它的核心价值是：

```text
┌──────────────────────────────────────┐
│ 1. 主链被拆成 prepare / execute / commit │
│ 2. 大块之间协议已经比较稳定          │
│ 3. reviewer 能明确判断改动边界       │
│ 4. 块内实现还能继续细拆              │
└──────────────────────────────────────┘
```

更直接一点：

```text
现在我们要改 retrieval
不应该去改 commit

现在我们要改 retry
不应该去改 AgentCore 协议

现在我们要改 dispatch
不应该去改 Reasoner 内部
```

这就是这套分块真正想得到的效果。

---

## 8. 当前还没完全收尾的地方

这套被动层已经基本成型，但还不是“所有边界都最终定案”。

当前还在收尾的主要是：

- `AgentLoop` 仍然是 runtime shell + composer
- 部分兼容 property 仍然保留
- `Reasoner.run_turn()` / `run()` 的最终长期边界还没完全定死

所以更准确的状态是：

```text
主骨架已成型
正在收尾
不是仍在混乱重构
```

---

## 9. 一句话总结

当前被动回复层可以概括成：

```text
AgentLoop 提供 runtime 入口
CoreRunner 负责分流
AgentCore 负责主编排
ContextStore 负责准备与提交
Reasoner 负责完整执行
```

也就是：

```text
大块抽象对接
块内实现可继续拆
主链协议逐步收稳
```

---

## 10. 用一条消息走完整个被动链

只讲抽象，读起来容易“知道分层，但没感觉”。

所以这里直接用一条消息走一遍当前主链。

### 10.1 示例消息

假设用户发来一条 Telegram 消息：

```text
“帮我看看明天北京天气，如果下雨提醒我带伞”
```

系统里首先拿到的是一条统一的 `InboundMessage`：

```text
InboundMessage
├─ channel = "telegram"
├─ chat_id = "123"
├─ sender = "user"
└─ content = "帮我看看明天北京天气，如果下雨提醒我带伞"
```

### 10.2 第一步：进入 AgentLoop

`AgentLoop._process()` 收到这条消息。

它这一层不负责“想怎么回答”，只负责：

- 把它当成一次被动 turn
- 交给 `CoreRunner`
- 管 runtime 级别的 timeout / processing state

可以理解成：

```text
AgentLoop
└─ 这条消息要不要处理？
   要。
   交给 CoreRunner。
```

### 10.3 第二步：CoreRunner 分流

`CoreRunner.process()` 先判断：

```text
这是不是 spawn completion 内部事件？
```

这条是普通用户消息，所以直接走：

```text
CoreRunner
└─ AgentCore.process()
```

这层的意义是让外层 runtime 不需要知道：

- 普通消息怎么跑
- 内部事件怎么跑

### 10.4 第三步：AgentCore 开始主编排

`AgentCore.process()` 开始串主流程。

它做的第一件事是：

```text
session = session_manager.get_or_create(key)
```

也就是先找到这条消息对应的会话。

然后进入：

```text
ContextStore.prepare()
```

### 10.5 第四步：ContextStore.prepare 准备输入

`prepare()` 会做这几件事：

```text
1. 读取 session history
2. 调 retrieval pipeline
3. 收 skill mentions
4. 生成 ContextBundle
```

如果这条天气消息命中了长期记忆或流程记忆，这一步会把相关内容整理进：

- `retrieved_memory_block`
- `retrieval_trace_raw`
- `skill_mentions`

最后产出：

```text
ContextBundle
├─ history
├─ skill_mentions
├─ retrieved_memory_block
└─ retrieval_trace_raw
```

这一步的意义是：

```text
把“本轮需要喂给执行层的输入”
一次性准备好
```

### 10.6 第五步：AgentCore 补 prompt 预览和 tool context

拿到 `ContextBundle` 后，`AgentCore` 会：

```text
1. build_system_prompt(...)
2. tools.set_context(channel, chat_id)
```

这里的 `tool context` 很重要。

因为后面如果真的调用天气工具、定时提醒工具，工具运行时要知道：

- 当前来自哪个 channel
- 当前属于哪个 chat_id

### 10.7 第六步：Reasoner.run_turn 执行完整被动 turn

然后进入真正的执行层：

```text
reasoner.run_turn(...)
```

对于这条天气消息，`run_turn()` 负责的是：

```text
1. build retry plan
2. trim history / sections
3. build preflight
4. 调 run()
5. 产出 TurnRunResult
```

也就是说，这一层负责“这一整轮怎么执行”，而不只是“调一次模型”。

### 10.8 第七步：Reasoner.run 做底层 tool loop

`run_turn()` 内部会继续调用：

```text
run()
```

这一层做的是低层 tool loop：

```text
1. 带着当前 messages 调模型
2. 如果模型要调工具，就执行工具
3. 如果模型调用 tool_search，就解锁更多工具
4. 如果出现重复调用，就做 repeat guard
5. 如果达到上限，就做 incomplete summary fallback
```

比如在天气这个例子里，可能发生的是：

```text
模型先决定调用天气工具
-> 工具返回“明天北京有雨”
-> 模型生成最终回复
```

也可能发生的是：

```text
模型先 tool_search 找天气相关工具
-> 解锁
-> 再调用真实天气工具
-> 再生成回复
```

最后 `run_turn()` 会收成一个统一结果：

```text
TurnRunResult
├─ reply
├─ tools_used
├─ tool_chain
├─ thinking
└─ context_retry
```

### 10.9 第八步：AgentCore 把执行结果交给 commit

`AgentCore` 拿到 `TurnRunResult` 后，不自己落盘，不自己发消息，而是统一交给：

```text
ContextStore.commit()
```

传进去的就是这一轮执行产物：

- `reply`
- `tools_used`
- `tool_chain`
- `thinking`
- `retrieval_raw`
- `context_retry`

### 10.10 第九步：ContextStore.commit 提交这一轮结果

`commit()` 会统一做：

```text
1. session append
2. observe trace
3. post_turn schedule
4. post_turn_actions
5. meme decorate
6. outbound dispatch
```

对这条天气消息来说，它最终会：

- 把 user / assistant 两条消息写进 session
- 记录 trace
- 安排 post-turn 后台动作
- 生成最终 `OutboundMessage`
- 通过 outbound 发回 Telegram

### 10.11 最终结果

所以从这条消息的视角看，整条链是：

```text
用户消息
-> AgentLoop
-> CoreRunner
-> AgentCore
-> ContextStore.prepare
-> Reasoner.run_turn
-> Reasoner.run
-> ContextStore.commit
-> OutboundMessage
```

而从职责视角看，是：

```text
AgentLoop      管入口
CoreRunner     管分流
AgentCore      管编排
ContextStore   管准备与提交
Reasoner       管执行
```

这就是现在这套被动回复层最核心的设计。
