## Purpose

把 C6 reconciliation 的作用域边界写成可验收契约：对账只能在自己租户的 blob 根内工作，
「有 metadata 行」即视为已知（含尚未被消息引用的 staged 终态），在途写入受宽限期保护，
演练形态（dry-run）必须零变更。这些边界在 C6 只由实现隐含表达，因而被写反过
（已知集合只取 committed + 全局扁平根 → 上传的附件被自己的对账删除、并跨租户互删）。

## ADDED Requirements

### Requirement: reconciliation 作用域限定与在途写入保护

reconciliation SHALL 只在**该租户的 blob 根**内枚举与删除文件，SHALL NOT 与其他租户共用扁平根；
「已知集合」SHALL 覆盖该租户 metadata 的**全部状态**（含 `staged`），因此「blob 位于最终路径且
metadata 行为 staged」这一上传成功终态 SHALL NOT 被判为 orphan。刚完成落盘而 metadata 尚未提交
的在途写入 SHALL 在可配置的宽限期（`orphan_grace_seconds`，默认 900 秒）内不被删除。
`dry_run` 演练 SHALL 只产出统计报告，SHALL NOT 删除任何文件或变更任何 metadata 状态。

#### Scenario: 上传后的附件经过对账仍可取回

- **WHEN** 客户端上传附件成功后（尚未被任何 message 引用），系统执行一轮 reconciliation
- **THEN** 该 blob 不被删除、metadata 不被标记 missing，随后按 attachment_id 读取仍返回原始字节

#### Scenario: 一个租户的对账不影响其他租户

- **WHEN** 租户 A 与租户 B 各有在库附件，系统只对租户 A 执行 reconciliation
- **THEN** 租户 B 的 blob 与 metadata 完全不被枚举、不被删除，读取仍成功

#### Scenario: 在途写入不被宽限期内的对账删除

- **WHEN** 某附件字节已落盘到最终路径但 metadata 行尚未提交，此时执行 reconciliation
- **THEN** 落盘时间在 `orphan_grace_seconds` 内的文件不判 orphan；超过宽限期后才被清理

#### Scenario: dry-run 演练零变更

- **WHEN** 以 `dry_run=true` 执行 reconciliation
- **THEN** 报告给出 will-delete 计数，磁盘文件与 metadata 状态均无任何变化，重跑结果一致
