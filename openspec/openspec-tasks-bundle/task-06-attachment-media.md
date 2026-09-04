# Task-06 — authenticated attachment/media lifecycle（attachment-media）

> 编号对应 PILOT_ROADMAP §5.9.10 第 6 项。状态标记复用 §8。跨阶段：P1 主体 + P3 恢复演练收尾。

## 元数据

- **所属阶段**：主要里程碑 = P1；恢复演练 = P3（blob↔metadata reconciliation / missing blob 告警演练）
- **§5.9 引用**：§5.9.15（attachment/media lifecycle）、§5.9.12（backup manifest 纳入 blob root）、§10 PROPOSED DEFAULT（Attachment policy）
- **§6 出口条件引用**：P1 出口「HTTP、上传、媒体和 WebSocket 统一接入认证；attachment/media 按 5.9.15 使用 tenant ownership、immutable attachment id、MIME/size limit 和清理策略」；P3 出口（恢复演练）
- **状态**：planned

## 目标

实现认证后的 attachment/media 生命周期：immutable `attachment_id` + tenant blob namespace + PG metadata ownership（owner/size/MIME/checksum/storage key/status/retention）；MIME sniffing + allowlist（图片/纯文本/PDF 初始建议）+ 单文件 20MiB（均配置化，§10 PROPOSED DEFAULT 于 P-1 确认）；可执行内容默认拒绝；未引用/失败临时上传 24h 清理；blob↔metadata reconciliation（落库成功 blob 缺失 → 错误状态告警；blob 存在 metadata 未提交 → cleanup）；blob root 进入 backup manifest（与 C12 对齐）；HTTP/WS/tool/channel envelope 不传播客户端可控本地绝对路径。

## 输入

- 上游 change 产出：C5（auth principal）、C1（tenant 派生）
- roadmap 冻结决策：§5.9.15 全节、§5.9.12（blob root 进 backup manifest）、§10 PROPOSED DEFAULT（Attachment policy 精确 MIME/扩展名/大小/TTL 于 P-1 接受或修改）
- 现有代码锚点：`AttachmentStore` 当前实现（workspace uploads / `/tmp/nexus_uploads` 本地文件，无统一 owner/metadata/reconciliation）
- 依赖前置：C5（D3）

## 输出

- 端点：上传/读取/转发/删除 API（全部重新校验 principal 与 ownership）
- DB schema：`attachments` 表（owner/size/detected MIME/checksum/storage key/status/引用 message/retention deadline）+ 迁移
- 代码：服务端 MIME sniffing + 扩展名规范化 + filename/path 隔离（不执行上传内容）、cleanup job（24h 临时文件清理）、reconciliation 流程
- 测试/证据：API 响应断言、越权负向测试、上传测试矩阵、cleanup job 测试、reconciliation 测试、manifest 覆盖检查

## 验收标准

- [ ] HTTP/WS/tool/channel envelope 不传播客户端可控本地绝对路径；只返回 immutable `attachment_id` — 验证：API 响应断言
- [ ] ownership 越权：A tenant 无法读/删 B tenant 附件 — 验证：跨 tenant 负向测试
- [ ] MIME sniffing + allowlist（图片/纯文本/PDF）+ 单文件 20MiB + 可执行拒绝；均配置化 — 验证：上传测试矩阵
- [ ] 未引用/失败临时上传默认 24h 清理 — 验证：cleanup job 测试
- [ ] blob↔metadata reconciliation：落库成功 blob 缺失 → 错误状态告警；blob 存在 metadata 未提交 → cleanup — 验证：reconciliation 双向测试
- [ ] blob root 进入 backup manifest（与 C12 对齐） — 验证：manifest 覆盖检查
- [ ] P3：attachment 恢复演练 + orphan/missing blob reconciliation 演练 — 验证：恢复演练报告
- [ ] 本 task 不触碰 auth 端点（C5）、工具 path resolver（C7） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：越权负向、MIME/可执行拒绝、reconciliation 双向必须有可复现测试；manifest 覆盖以 C12 模板校验为准，不能只在文档里写「已纳入」。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`attachments` 表、tenant blob namespace、cleanup job、reconciliation 流程、上传/读取/转发/删除 API
- 本任务不触碰：auth 端点与 session（C5，只消费 principal）、工具 path resolver（C7）、backup manifest 模板本体（C12，只提供 blob root 条目）
- 共享 seam 协议：消费 C5 认证 principal；blob 路径 tenant-namespaced（§5.9.12）；blob root 随 C12 backup manifest 恢复（§5.9.12 一致性点）

## 依赖

- **左依赖（必须先完成）**：C5（D3：attachment 对普通 tenant 开放的前置）
- **右依赖（本任务前置于）**：无独立下游 change（与 C12 backup 关联）；P3 恢复演练归本 task 收尾
- **可并行**：C9 / C10 / C11 / C14（均为 C5 的下游，互不依赖 C6）

## 风险与需冻结决策

- §10 PROPOSED DEFAULT「Attachment policy」：P-1 接受或修改精确 MIME/扩展名/大小/TTL，并固化 sniffing/reconciliation 测试；默认值：图片/纯文本/PDF、20MiB/文件、24h cleanup、可执行拒绝。
- §5.9.15：`/tmp` 不属于 durable backup 范围（§5.9.12）；blob 缺失必须告警而非静默。
- 风险：blob root 漏进 backup manifest → 验收第 6 条要求以 C12 manifest 校验为准强制对齐。