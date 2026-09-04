# Task-04 — WebChat protocol + dev-only channel/Gateway/frontend（webchat-protocol-dev-loop）

> 编号对应 PILOT_ROADMAP §5.9.10 第 4 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P0.5（dev-only WebChat 最小可用闭环）
- **§5.9 引用**：§5.9.4（WebSocket 协议/游标/慢消费者）、§5.9.1（WS replay 硬冲突）、§5.9.5（per-connection outbound）、§5.6（双入口同步验收）、§10 OPEN FOR P-1 SPEC（WebSocket frame/error schema）
- **§6 出口条件引用**：P0.5 出口「本地/dev 下打开 WebChat 收发消息、收到流式更新；刷新/断线重连不重复；消息顺序稳定；异常连接清理；协议测试通过」「dev-only 暴露门禁，P1 前不得公网」
- **状态**：planned

## 目标

实现 WebChat channel adapter（WebSocket 入站 → 内部消息；AgentLoop 回复/流式片段/工具状态/终态 → 对应连接）；Gateway HTTP/WS 路由 + 心跳 + 断线清理 + 重连；`hello/send/delta/completed/error/replay` 协议 + `client_message_id` 幂等 + 慢消费者策略；WebChat 前端 bundle（消息列表/输入/发送状态/流式展示/连接状态/重连/错误提示）；**dev-only 门禁**——本地或显式 dev mode 临时单用户身份，无 P1 认证与 tenant 隔离前不得公网暴露。

## 输入

- 上游 change 产出：C1（canonical message/sequence + identity resolver）、C2（durable inbox/outbox，E1）、C3（tenant-scoped admission + 有界队列，E2）
- roadmap 冻结决策：§5.9.4（WS 协议语义）、§5.9.1（WS replay：final durable、delta 非 durable、慢消费者降级为游标补拉）、§5.9.5（per-connection outbound 限幅）、§5.6（双入口同步）
- 现有代码锚点：`bootstrap/chat_api.py`（WebChat 骨架）、`bootstrap/app.py`（应用挂载点）
- 依赖前置：C1 + C2(E1) + C3(E2)

## 输出

- 代码：`infra/channels/web_chat_channel.py`（WS 入站→内部消息 / AgentLoop→连接）、WS 路由（HTTP/WS + 心跳 + 断线清理 + 重连）、慢消费者策略、dev-only feature flag
- 前端：WebChat 前端 bundle（消息列表/文本输入/发送状态/流式回复/连接状态/重连/错误提示）
- 契约 fixture：协议 contract fixture（`hello/send/delta/completed/error/replay` JSON 帧/预期响应/错误案例，**前后端共用同一组向量**）
- 测试/证据：端到端 dev 测试、重连补拉测试、慢消费者测试、协议断言、非 dev 暴露阻断测试、断线不取消 turn 测试

## 验收标准

- [ ] 本地/dev 打开 WebChat 发送消息并收到 AgentLoop 回复 + 流式更新 — 验证：端到端 dev 测试（真实入口，dev mode）
- [ ] 重连补拉无重复、顺序稳定（按 `last_sequence` 游标，final 从 canonical message 补拉） — 验证：重连补拉测试（断线→重连→断言无重复、无乱序）
- [ ] 协议 contract fixture 前后端共用（同组 JSON 帧/预期响应/错误案例双向执行） — 验证：fixture 双向测试（前端脚本 + 后端测试等同一向量文件）
- [ ] 慢消费者：192 event → 丢弃 delta + `replay_required`；256 event 或 1MiB → 明确 overload close code（§5.9.5） — 验证：慢消费者测试矩阵
- [ ] `hello` 携带 connection_id / 账号 / 规范会话 / 最新序号 / 协议版本；客户端 payload 不得声明可信 tenant_id（§5.9.1 / §5.9.4） — 验证：协议断言
- [ ] 非 dev 模式不可暴露公网（feature flag 门禁；无 P1 认证与 tenant 隔离前不得公网） — 验证：非 dev 启动 + 对外暴露阻断测试
- [ ] 断线只影响显示，不取消服务器端 turn/tool（§10 DECIDED 工具取消） — 验证：断线执行继续测试
- [ ] 异常连接能够清理（心跳超时/静默连接回收） — 验证：连接生命周期测试
- [ ] 本 task 不触碰 auth 端点（C5）与 Telegram binding（C10） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：协议 fixture 双向测试 + 端到端 dev 测试必须真实运行通过；「断线不取消 turn」「不暴露公网」两条负向语义必须断言而非仅靠文档。

## 独立性边界（不与其他任务重复）

- 本任务拥有：WebChat channel adapter、Gateway WS 路由、前端 bundle、协议 contract fixture、慢消费者策略、dev-only feature flag
- 本任务不触碰：auth 端点与 session（C5）、Telegram binding/同步（C10）、durable 表 schema（C2，只消费）、admission 调度策略（C3，只消费）
- 共享 seam 协议：消费 C1 resolver 得可信 tenant（客户端 tenant/chat/session 字段不参与授权）；消费 C2 durable inbox/outbox；消费 C3 admission；为 C5（E3）提供 WebChat channel 承载认证；为 C10（E8）提供实时推送通道；为 C14（D7）提供 WebChat 前端承载 selector

## 依赖

- **左依赖（必须先完成）**：C1（D1）+ C2（E1：durable inbox/outbox）+ C3（E2：admission + 有界队列）
- **右依赖（本任务前置于）**：C5（E3：P1 公网 WebChat = C4 通道 + C5 认证）、C10（E8：Telegram 新消息实时推送到 WebChat）、C14（D7：WebChat 前端承载 memory engine selector）
- **可并行**：C13（D6 独立，不阻塞 C4）

## 风险与需冻结决策

- §10 OPEN FOR P-1 SPEC「WebSocket frame/error schema」：`hello/send/delta/completed/error/replay` 语义已冻结，**精确 JSON 字段/版本/错误码/close code** 须在 P0.5 开工前提交 protocol fixture 与 contract test vectors —— 本 task 的「契约 fixture」即该前置产出。
- §10 DECIDED「消息顺序与幂等」：客户端 `client_message_id` 是强制幂等键（§5.9.11），不允许只靠连接顺序或内存去重。
- 风险：慢消费者策略（delta degrade vs overload close）边界易混 → 用 192/256/1MiB 三档测试固定；dev 门禁被绕过 → 默认关闭 + 显式 enable 才开放。