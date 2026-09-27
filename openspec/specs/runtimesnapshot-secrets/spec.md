# runtimesnapshot-secrets Specification

## Purpose

定义 Pilot 运行时的全入口 work-start RuntimeSnapshot lease、副作用前 revocation recheck（fail-closed）、hook failure 分层（gate fail-closed / fanout 有界 timeout）、per-task tenant runtime plan 接缝与 contribution 元数据，以及 tenant secret 静态加密与 rotation。依据 PILOT_ROADMAP §5.9.16 / §5.9.7 / §10 DECIDED 冻结决策；不建设插件签名/扫描/sandbox。

## Requirements

### Requirement: 全入口 work-start snapshot lease

Passive、Proactive、Drift（经 proactive tick）、consolidation、optimizer 与 plugin job 的每个 work SHALL 在 work start 时恰好取得一次 RuntimeSnapshot lease 并绑定到执行上下文，work 结束（含异常/取消）SHALL 释放。进行中的 work SHALL NOT 因 snapshot 热更新切换 snapshot；recovery 启动扫描（无插件交互的同步扫描）不要求 lease，但 SHALL 在 audit 报告中显式记录。

#### Scenario: passive work 持有 lease

- **WHEN** 一条渠道消息进入 passive turn 执行
- **THEN** turn 执行上下文内可解析出非空的当前 RuntimeSnapshot，且其 snapshot_id 与 work 开始时 store 的 committed snapshot 一致；turn 结束后 lease 计数归零

#### Scenario: 进行中 work 不切 snapshot

- **WHEN** 某个 work 持有旧 snapshot lease 执行期间，store 发布并 commit 新 snapshot
- **THEN** 该 work 的执行上下文内解析到的 snapshot 仍是旧 snapshot；work 结束后新取得的 lease 解析到新 snapshot

#### Scenario: maintenance work 持有 lease

- **WHEN** 后台 consolidation worker 或 memory optimizer 执行一次 maintenance work
- **THEN** work 执行上下文内解析到非空当前 RuntimeSnapshot，结束（含异常）后 lease 释放

### Requirement: 副作用前 revocation recheck（fail-closed）

在被动 work 开始、proactive tick 开始、schedule 直推副作用与 plugin job 执行前，系统 SHALL 重新读取当前账号/策略状态并据此放行或拒绝。状态为 `revoked`/`suspended`、`unknown` 或状态源异常时 SHALL 拒绝（fail-closed）；状态源未接线（Pilot dev）SHALL 放行并记录结构化日志，SHALL NOT 静默降级为「看似已检查」。持有旧 snapshot 或旧 policy revision 的 work SHALL NOT 绕过该 recheck。

#### Scenario: 旧 snapshot 持有期间账号被撤销

- **WHEN** 某 work 持有旧 snapshot lease 执行中，其账号状态变为 `revoked`，该 work 到达副作用点
- **THEN** 副作用被类型化拒绝（携带 tenant 与 action），副作用未发生；拒绝依据来自 recheck 的当前状态而非 work 捕获状态

#### Scenario: 状态源异常 fail-closed

- **WHEN** 状态 provider 查询抛出异常或返回 `unknown`
- **THEN** 副作用点拒绝执行，异常不向外扩散为未处理崩溃

#### Scenario: 状态源未接线时显式 dev-open

- **WHEN** Pilot 未接入账号状态源（provider 为空）且 work 到达检查点
- **THEN** work 放行，并产生标记 `revocation_gate=dev_open` 的结构化日志

### Requirement: hook failure 分层

gate/interceptor 型 hook（pre-tool、生命周期 phase module、EventBus emit intercept）在超时、异常或上下文缺失时 SHALL fail-closed：pre-tool SHALL 被受控拒绝（显式 deny 或携带 hook 失败信息的受控 error 结果），真实工具 SHALL NOT 执行，phase module 异常中断当前 turn；fanout/telemetry 型 hook（post-tool、EventBus observe/fanout 观察者）SHALL 使用有界 timeout（默认 5.0s，可配置），单观察者超时/异常 SHALL 被记录且 SHALL NOT 中断主链路、SHALL NOT 反向改写已提交的业务终态。

#### Scenario: pre-tool hook 超时拒绝工具调用

- **WHEN** 某插件 pre-tool hook 执行超过有界 timeout
- **THEN** 该次工具调用被受控拒绝（携带 hook 名称与超时信息的 error 结果），真实工具未执行，主链路不崩溃

#### Scenario: post-tool hook 超时不影响工具结果

- **WHEN** 某插件 post_tool_use hook 执行超过有界 timeout
- **THEN** 超时被记录为 hook 失败，工具结果与 turn 终态保持不变，后续 hook 与主链路继续

#### Scenario: post_tool_error hook 失败不掩盖工具错误

- **WHEN** 工具执行抛错，且某 post_tool_error hook 随后超时或异常
- **THEN** 工具本身的错误信息保持为终态输出，hook 失败仅记录在 trace 中

#### Scenario: 观察者异常隔离

- **WHEN** EventBus 某个 observe/fanout 观察者抛出异常或超时
- **THEN** 事件分发继续送达其余观察者，emit 方不感知失败（除记录外）

### Requirement: per-task tenant runtime plan 与 contribution 元数据

系统 SHALL 提供 `TenantRuntimeResolver`：按 (`snapshot_id`, `tenant_id`, `tenant_policy_revision`) 与 tenant bindings 解析出不可变 `TenantRuntimePlan`；相同输入 SHALL 产出等价 plan。每个可选能力项 SHALL 具备稳定 `contribution_id`、固定 hook/tool/job 类型、`binding_policy=required|default_on|opt_in` 与 `tenant_configurable` 声明；未进入 plan 的 contribution SHALL 不可见、不可调用、不可被后台任务隐式触发。每次 hook/tool/job 调用 SHALL 通过显式 `PluginInvocationContext` 携带 plan 与 work 上下文；插件实例 SHALL NOT 保存跨 await 的当前 tenant 状态，`ContextVar` 仅作观测、SHALL NOT 作为授权边界。

#### Scenario: opt_in contribution 未绑定不可见

- **WHEN** 某 `opt_in` contribution 未出现在 tenant bindings 中，resolver 解析该 tenant 的 plan
- **THEN** 该 contribution 不在 plan 的 enabled 集合内，`plan.allows(contribution_id)` 返回 false

#### Scenario: plan 不可变且可复现

- **WHEN** 以相同 snapshot、tenant、revision 与 bindings 连续两次解析
- **THEN** 两次产出等价的 plan（相同 snapshot_id/tenant_id/revision/enabled 集合），且任一次解析结果不被后续解析修改

### Requirement: tenant secret 静态加密与 rotation

tenant secret SHALL 以 AES-256-GCM 静态加密后落盘，sealed 值 SHALL 内嵌版本与 key_id（`v1:<key_id>:<nonce>:<ciphertext>`）；解密 SHALL 按 sealed 值内嵌 key_id 选择密钥并以 GCM 认证标签校验完整性。rotation SHALL 通过新增密钥文件并切换 active 指针完成：rotation 后新数据用新密钥加密、旧 sealed 值仍可解密；密钥撤销后对应 sealed 值 SHALL 解密失败。tenant secret 明文 SHALL NOT 进入 tool schema、模型可见参数、普通日志、metrics label 或错误字符串。

#### Scenario: 加密往返与明文不泄漏

- **WHEN** 对一段明文执行 encrypt 后再 decrypt
- **THEN** 解密结果与明文一致，且 sealed 值不包含明文的任何子串

#### Scenario: rotation 后旧数据仍可解、撤销后不可解

- **WHEN** 新增第二把密钥并切换 active 指针，用新密钥加密新数据；随后撤销第一把密钥
- **THEN** rotation 前加密的数据仍可解密，rotation 后加密的数据使用新 key_id，撤销第一把密钥后其 sealed 值解密失败（认证失败）

#### Scenario: 篡改的 sealed 值被拒绝

- **WHEN** sealed 值的密文或 nonce 任一字节被篡改后执行解密
- **THEN** 解密失败（认证失败），不返回部分明文

### Requirement: snapshot 发布失败保留旧 committed snapshot

snapshot compile/publish 失败时系统 SHALL 继续使用上一份 committed snapshot：失败候选 SHALL NOT 成为 current、SHALL NOT 被部分发布，当前可用 snapshot SHALL NOT 被清空；失败 SHALL 记录 generation/error。

#### Scenario: 非法候选发布失败后旧 snapshot 继续服务

- **WHEN** 一个包含非法 contribution（如 Channel 名称冲突）的候选发布失败
- **THEN** store 的 current 仍是旧 committed snapshot，新 lease 可获取且内容为旧 snapshot，失败候选进入 aborted 并被 drain
