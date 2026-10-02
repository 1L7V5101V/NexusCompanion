# attachment-media Specification

## Purpose

定义带队 Pilot 附件（attachment/media）全生命周期契约：上传只返回 immutable `attachment_id`，
blob 以 tenant 命名空间落盘、metadata 与归属在 PostgreSQL，内容接受面 fail-closed（精确 MIME/扩展名 allowlist、
size/像素/文本硬上限，扩展名与服务端 sniff 双一致），读取/转发/删除重新校验 principal 与 ownership，
引用保留（30 天已引用 / 24 小时临时）与 orphan/missing reconciliation，blob root 进入 backup manifest，
并产生符合 §7.1 边界的事件记录点。上游契约（C7 租户路径、C5 认证、C1 身份派生）不重述。

## ADDED Requirements

### Requirement: 上传只返回 immutable attachment_id 且不传播本地路径

上传入口 SHALL 在内容校验通过后返回 immutable `attachment_id`（服务端生成、不可由客户端指定），
以及按该 id 构造的读取 url；HTTP 响应、WebSocket 帧、工具调用与 channel envelope SHALL NOT
传播可由客户端控制的本地绝对路径。attachment 的物理存储键（storage key）SHALL 由服务端派生并与
`attachment_id` 一一对应，客户端 SHALL NOT 以路径或 storage key 直接寻址。

#### Scenario: 上传成功响应不含本地路径

- **WHEN** 客户端向上传入口提交一个格式与大小均在允许范围内的文件
- **THEN** 响应只含 `attachment_id` 与按该 id 构造的 `url`，响应正文与任何 header 均不含本地文件系统路径

#### Scenario: 客户端指定的 id 或路径被忽略

- **WHEN** 客户端在上传请求中携带自定义 id、storage key 或本地路径
- **THEN** 服务端忽略这些字段，使用自己生成的 `attachment_id` 与派生路径，响应不回显客户端提交的路径

### Requirement: 上传内容接受面为冻结 allowlist 且双一致

服务端 SHALL 仅接受冻结 allowlist 内的内容：`image/jpeg(.jpg/.jpeg)`、`image/png(.png)`、
`image/webp(.webp)`、`image/gif(.gif)`、`text/plain(.txt)`（UTF-8，允许 BOM）。
服务端 SHALL 对内容做 MIME sniffing，且 SHALL 要求「客户端扩展名映射的期望 MIME」与「服务端 sniff
判定的 MIME」同时在 allowlist 且彼此一致才接受；任一不一致、不在 allowlist，或属于
PDF/压缩包/可执行内容/脚本/HTML/SVG/BMP/AVIF/HEIC SHALL fail-closed 拒绝并返回冻结错误码，
SHALL NOT 执行、转码或尝试解析上传内容。文本编码 SHALL 仅接受 UTF-8（含 BOM）；UTF-16/GBK 等
SHALL 拒绝并提示转码后重传。

#### Scenario: 扩展名与内容 sniff 不一致被拒绝

- **WHEN** 客户端提交一个扩展名为 `.jpg` 但内容 sniff 判定为文本（或反之）的文件
- **THEN** 请求被拒绝，返回冻结错误码，不写入任何 blob 与 metadata，错误码可区分「类型不在 allowlist」
  与「扩展名与内容不一致」

#### Scenario: 禁止类型被拒绝

- **WHEN** 客户端上传 PDF、ZIP、可执行文件、SVG、HTML 或 BMP 内容
- **THEN** 请求被拒绝（冻结错误码），不产生 `attachment_id`，不留残留文件

#### Scenario: 非 UTF-8 文本被拒绝

- **WHEN** 客户端上传以 UTF-16 或 GBK 编码的 `.txt` 文件
- **THEN** 请求被拒绝并给出明确编码错误提示，不产生 attachment

### Requirement: 单文件与资源硬上限

上传 SHALL 应用冻结上限（默认值可配置，冻结默认如下）：单文件 ≤ 20 MiB；图片总像素 ≤ 16.8M
（≈4096×4096）、解码内存 ≤ 64 MiB、解码耗时 ≤ 5s、GIF 动画帧 ≤ 100 帧；文本 ≤ 200k 字符。
任一项超限 SHALL fail-closed 拒绝，返回对应冻结错误码，且解码过程 SHALL 受超时/内存边界保护，
不得因超大文件或畸形图片拖垮进程。

#### Scenario: 超过单文件大小上限被拒绝

- **WHEN** 客户端上行文件超过 20 MiB
- **THEN** 请求在内容持久化前被拒绝，返回 `upload_too_large`，不产生 blob 与 metadata

#### Scenario: 图片像素/帧/解码超限被拒绝

- **WHEN** 图片总像素超过 16.8M、GIF 帧数超过 100、或解码耗时超过 5s
- **THEN** 分别返回对应冻结错误码，进程继续可用，不产生 attachment

#### Scenario: 文本超长被拒绝

- **WHEN** `.txt` 内容超过 200k 字符
- **THEN** 请求被拒绝并返回冻结错误码

### Requirement: blob 落 tenant 命名空间且 metadata 全量入 PostgreSQL

附件字节 SHALL 存储在 tenant 命名空间的 blob 根下（复用 C7 attachments_root 与 tenant 目录清洗），
路径由服务端派生为 `{blob_root}/{tenant_dirname}/{attachment_id}.{server_ext}`；
SHALL NOT 使用 `/tmp` 或任何临时目录作为持久位置。PostgreSQL 的 attachment metadata SHALL 至少记录：
owner（account + tenant）、size、detected MIME、checksum（sha256）、storage key、状态、引用 message、
retention deadline 与时间戳。blob 落盘与 metadata 提交 SHALL 有明确的两段式流程，使「blob 存在但 metadata
未提交」可被识别为 orphan。

#### Scenario: 同一租户不同附件路径互不冲突且稳定

- **WHEN** 同一 tenant 上传 10 个附件
- **THEN** 每个附件落在该 tenant 目录内、文件名互不相同且等于 attachment_id 加服务端扩展名

#### Scenario: metadata 与 blob 一一对应可核验

- **WHEN** 以 attachment_id 查询 metadata
- **THEN** 返回 owner/tenant/size/detected MIME/checksum/storage key/status/retention deadline，且
  storage key 与磁盘实际路径可解析一致；checksum 在读取时可复算比对

### Requirement: 读取、转发、删除重新校验 principal 与 ownership

附件读取、转发（被新消息引用）与删除（解除引用）SHALL 以服务端派生 principal（account → tenant →
canonical conversation）重新校验归属；跨租户访问 SHALL 返回 404/403 且不泄露存在性。
账号 suspended/revoked 或 tenant 状态变化时，附件读取/转发 SHALL fail-closed。

#### Scenario: 跨租户读取被拒

- **WHEN** 租户 A 的登录主体请求读取租户 B 的 attachment_id
- **THEN** 请求被拒绝（404 或 403），不返回任何字节，也不返回「附件存在」的提示

#### Scenario: revoked 账号无法读取

- **WHEN** 账号已被 revoke，随后请求读取其原有 attachment
- **THEN** 请求被拒绝，附件不可被继续读取

### Requirement: 引用保留与临时清理

服务端 SHALL 维护 message ↔ attachment 引用（转发在同一附件上追加引用）；已引用 attachment 的
retention deadline SHALL 以「最后一次引用时间 + 30 天」计算，解除全部引用后按同样规则进入待清理；
未引用或失败的临时上传 SHALL 在 24 小时后清理。清理 SHALL 幂等（重跑不重复删除、不误删已引用项）。

#### Scenario: 解除全部引用后到期清理

- **WHEN** 某附件最后一条引用被解除，随后超过其保留期
- **THEN** 清理任务删除该 blob 与 metadata，重跑清理结果删除数为 0

#### Scenario: 临时上传 24 小时清理且已引用不受影响

- **WHEN** 存在未引用超过 24 小时的临时文件与已被引用 3 天的附件
- **THEN** 只有未引用的临时文件被清理，已引用附件保留

### Requirement: orphan 与 missing blob reconciliation

附件系统 SHALL 提供 reconciliation：metadata 已提交但 blob 缺失（missing）SHALL 被标记并告警；
blob 存在但无对应 metadata（orphan）SHALL 可被安全清理；二者 SHALL 不静默吞掉。reconciliation
SHALL 按 tenant 命名的有效范围执行，不扫描 `/tmp`。

#### Scenario: missing blob 被标记告警

- **WHEN** metadata 存在但磁盘 blob 缺失
- **THEN** reconciliation 输出缺失记录并告警，该 attachment 保持不可读状态而非静默返回空字节

#### Scenario: orphan blob 被清理

- **WHEN** blob 存在但无对应 metadata（含 staging 超龄文件）
- **THEN** reconciliation 将其识别为 orphan 并清理，清理重复执行次数为 0 时无副作用

### Requirement: blob root 进入 backup manifest

attachment blob root SHALL 作为 `tenant-workspace` manifest 条目的子路径显式登记（kind 集合不变），
并声明一致性点、校验方式与保留策略；manifest 模板与校验器随新增 canonical 存储同步更新并通过校验
（C12 §8.3）。

#### Scenario: manifest 含 attachment blob root 声明

- **WHEN** 使用更新后的 manifest 模板执行校验器
- **THEN** `tenant-workspace` 条目显式声明 attachment blob root 子路径、一致性点（与 PostgreSQL 恢复点对齐）
  与保留策略，校验通过，无条目命中 `/tmp` 之类临时目录

### Requirement: 附件生命周期事件记录点符合 §7.1 边界

上传、读取、删除/解除引用与清理 SHALL 产生结构化生命周期事件（含类型、状态、耗时、错误类别、
服务端派生归属标识）；事件字段 SHALL 为既有冻结字段集的子集（单一来源，不新增字段）；
filename、本地路径、内容原文与附件字节 SHALL NOT 进入事件、metrics label 或普通日志；
`attachment_id` 属高基数身份字段 SHALL NOT 进入 metrics label 白名单。

#### Scenario: 上传事件只含结构化字段

- **WHEN** 一次附件上传成功完成
- **THEN** 产生一条事件，含 status/size/耗时/归属 tenant 等结构化字段，不含 filename 原文、本地路径
  或内容预览

#### Scenario: 失败上传事件保留错误类别

- **WHEN** 一次上传因类型拒绝/超限/解码失败被拒
- **THEN** 产生一条失败事件，含冻结错误类别与耗时，不含客户端文件名与内容

#### Scenario: 附件标识不进 metrics label

- **WHEN** 以 `attachment_id` 作为 label 名注册指标
- **THEN** 注册失败（label 白名单拒绝），该事件身份字段只出现在带访问控制的记录面