# Turn 持久化与工具失败处理:设计讨论记录

日期:2026-09-05
状态:讨论性结论,未实现,无代码改动。本文件记录一次设计讨论的共识与待定项,供后续实现参考。

## 1. 背景:现状盘点(代码事实)

- 被动链路(WebChat/Telegram)的消息只在整轮成功后由 after_reasoning 落库:一条 user 加一条 assistant。整轮工具轨迹折进 assistant 消息的 tool_chain 列,下轮由 Session.get_history 反扩成 assistant tool_calls 加 role:tool。
- WS 的 message_accepted ack 只是进程内确认,不代表落库。client_message_id 幂等只在进程内,不跨崩溃。
- 模型调用失败的分流:asyncio.TimeoutError 走"流响应中断"兜底回复并照常落库;httpx/openai 的连接类错误直接抛到 pipeline,返回通用错误,整轮不落库。
- 进程崩溃:进行中与排队中的消息全部丢失,无 WAL / 无 outbox / 无 at-least-once。
- 控制面(SDK / ConversationRuntime)有 durable TurnRecord 状态机:turns 表加 create_turn / transition_turn 的 CAS,QUEUED 到 IN_PROGRESS 再到 COMPLETED / FAILED / INTERRUPTED / CANCELLED。但逐步 items 只在终态一次性快照,创建时只带 user item。硬崩溃留下一条无步骤的 IN_PROGRESS。
- D6 dual-store:PG-primary 生产下 turns 控制面刻意留在本地 SQLite workspace/turn_audit.db;纯 SQLite 模式与 messages 同库(sessions.db)。turn 审计日志(RoutingTurnLogger)只在成功轮次写 workspace/logs/{passive,proactive,drift}.db。
- 运行历史分三处:进行中只在内存;成功后折进 assistant 的 tool_chain 列;审计库只在成功轮次写旁路日志。

## 2. 核心问题(故障模型)

- 工具副作用发生在 turn 中途、先于落库。若随后模型调用失败或进程崩溃,整轮不落库,但副作用(例如已推送的消息)已经发生。账上无记录,用户重试会导致副作用重复执行。
- 工具轨迹只在整轮成功后才随 assistant 落库。中断即丢失,恢复时没有任何依据。
- 控制面账本只在终态快照步骤,无法支撑"进程死在工具执行中途"的恢复。

## 3. 讨论后一致的设计方向

### 3.1 执行门控:只有完整到达且已提交的 assistant 消息才执行工具

报错 / 中止 / 截断的消息永远不执行工具。"完整到达"以 provider 返回的 finish_reason 等于 tool_calls 且工具参数通过 schema 校验为准,不以流是否读完为准。

顺序反转:先 append assistant 工具消息进日志 / 历史,写成功后才执行工具,工具结果逐条 append。区别于现在的"先执行、轮末整体落库"。这条让"日志即意图":完整到达的 assistant 消息本身就是工具执行的账目,不需要单独的意图表。

### 3.2 不搞事务,收敛

- LLM 调用可安全重试,因为失败的消息从不进上下文。
- 文件写入收敛:全量覆盖加串行,不需要 journal。注意必须原子替换:写临时文件,fsync,再 os.replace 到目标路径。直接覆盖原文件在断电时会产生新旧参半的破文件,两个版本都不收敛。
- 进程崩溃用 append-only 日志加 resume 兜底。
- bash、peer_agent、subagent 这类真正不可幂等或间接产生副作用的操作不对用户开放。message_push 是发消息的核心能力,保留。

### 3.3 message_push 的收敛策略

message_push 不可幂等,目标是外部渠道,做不了目标端去重。收敛策略定为一条死规矩:resume 从不自动重发推送。遇到"assistant 已要求推送、但结果条目缺失"的情况,如实处理为"可能已发送",由模型询问用户,而不是自己再推一遍。代价是极罕见情况下用户收到两条,换来的是不需要猜。是否给每次推送加内容指纹让模型识别重复补发,待定。

### 3.4 会话日志的结构

- 每租户一份两个版本(A/B)的 turn 级日志,copy and write 更新,切换走原子替换,任何时刻至少有一版完整可读。
- 只保留当前 turn 的日志。历史与会话账本分离:历史是所有旧 turn 的内容,模型记忆的来源,长期保留,现由 messages 表承担;账本是当前这个 turn 执行到哪一步,只服务崩溃恢复。turn 到达终态且持久产物已写入历史后,账本即可丢弃。写历史在前,删账本在后。
- 单位拆到当前 turn 后,A/B 拷贝成本从"随日志总量线性涨"变为"随单轮封顶",不受历史长度影响。

### 3.5 中断的 assistant 消息处理边界

带 tool_calls 却无配对 role:tool 结果的消息不能原样进历史,协议要求结果紧随声明它的消息。倾向整轮废弃,悬挂的 user 消息表现为无回复,与现状失败表现一致。

### 3.6 发送前归一化兜底

所有发给 provider 的历史先过一道:凡是孤儿 tool call(声明了但没有结果的),就地补一条"未执行 / 出错"的结果。职责划分:这条不管工具执不执行,只管被模型看到的任何工具调用都带着明确结果,保证协议自洽,模型不会把中断误判成可重试。

落地时注意两点:补全做在共享层、用内部消息形状(role tool 加 tool_call_id),不要只做在某一个 provider 的专属转换里;补的结果要紧贴声明它的 assistant 消息之后,不能隔段补。归一化不是"不执行工具"的机制,不执行靠 3.1 的门控,归一化是给漏网之鱼善后的兜底。

## 4. 待定决策

1. 中断的 assistant 消息:整轮废弃(默认倾向)还是补"未执行"后再入历史。
2. 会话日志切分时机:当前单租户 A/B 整份可行,何时拆到会话粒度(5000 人方向,租户只做目录)。
3. message_push 是否加内容指纹。
4. turn 级多租户隔离:现状 turns 表无 tenant 隔离,只靠 thread 唯一,是否给账本 / 日志做隔离。
5. 归一化兜底目前无孤儿来源(get_history 与工具循环都保证配对),它主要服务 3.1 新模型下的部分持久化,是否现在就做。

## 附:关键代码位置

- agent/core/passive_turn.py:reasoner 工具循环,执行先于落库的现状所在。
- agent/lifecycle/phases/after_reasoning.py:整轮成功后统一落库的位置。
- agent/lifecycle/phases/after_turn.py:成功后写审计日志。
- session/manager.py:Session.get_history,tool_chain 反扩,保证配对。
- session/store.py:turns 表与 create_turn / transition_turn CAS。
- agent/control/runtime.py:ConversationRuntime,逐步 items 只在终态快照。
- agent/model_runtime/transports/responses_converters.py:Responses 顺序映射,无孤儿补全。
- agent/tools/message_push.py:message_push 发送实现。
- bootstrap/tools.py:turn 审计日志库路径(passive / proactive / drift)。
