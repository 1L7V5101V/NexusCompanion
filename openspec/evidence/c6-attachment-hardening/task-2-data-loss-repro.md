# Task 2/3 — 两处静默数据销毁的复现与修复后证据（2026-10-02）

命令环境：本地 PG 5433（`nexus` 管理库 + `nexus_c6test` scratch），
`NEXUS_REQUIRE_PG=1 .venv/Scripts/python.exe -m pytest -q -W error tests/attachments/`

## 复现（修复前，main HEAD `76209a5d`（原 `bc24797f`），临时用例已删除）

用两个临时用例直接跑真实 `AttachmentService` / `AttachmentLifecycleRuntime`：

### 缺陷 2：per-tenant 对账在全局扁平根上跨租户删除

```
上传后 metadata.status = 'staged'（契约期望 committed）
上传后 blob 存活: True
fetch 字节一致: True
reconcile -> {'tenant_id': 'dev', 'marked_missing': 0, 'removed_orphans': 1, ...}
对账后 blob 存活: False        ← 同租户自己的附件被删
```

三租户（tenantA/B/C）各一个在库附件，跑一次启动轮 `_run_round(0)`：

```
启动对账后各租户 blob 存活: {'tenantA': False, 'tenantB': False, 'tenantC': False}
```

机制：每轮 `cleanup_orphans(known_keys=本租户 committed 键)` 删除根下不属于自己的全部文件；
按 A→B→C 遍历时，A 的文件在 B、C 轮里被删，最终全覆盖。触发条件为每次容器启动。

### 缺陷 1：上传终态 staged 不在 known 集合内

`list_committed_by_tenant` 只返回 `committed/missing`，上传成功写入的是 `staged`，
因此任何已上传附件都在 `known_keys` 之外 → 判孤儿。

## 定性纠偏（为什么不改成「上传即 committed」）

`§5.9.15` 与 P-1 冻结值是「未引用/失败临时上传 24h 清理，已引用自最后引用起 30d」。
上传即 commit 会给未引用附件 `referenced_ttl_days=30` 的 deadline，反而违反冻结值。
`staged`（已入库、尚无 message 引用）+ blob 在最终路径是**正确的上传终态**，
错的是 known 集合的过滤条件。故修复方向：`list_by_tenant` 覆盖全部状态 + 在途宽限期。

## 修复后的守卫用例（均失败于修复前实现）

| 用例 | 断言 |
| --- | --- |
| `test_blob_store.py::test_blob_root_is_tenant_namespace` | 落盘路径含 `tenants/<dirname>/attachments` |
| `test_blob_store.py::test_tenants_are_physically_disjoint` | A 的孤儿集合不含 B 的文件，A 的清理后 B 仍可 `read_bytes` |
| `test_blob_store.py::test_missing_tenant_id_fails_closed` | 空 tenant_id 直接 `AttachmentStorageError` |
| `test_blob_store.py::test_orphan_grace_spares_in_flight_upload` | 宽限期内不判孤儿；拨老 mtime 后才收敛 |
| `test_lifecycle.py::test_reconcile_keeps_uploaded_staged_attachment` | staged+blob → `removed_orphans == 0` |
| `test_lifecycle.py::test_reconcile_does_not_touch_other_tenant` | 对账 tenant-a 后 tenant-b 字节原样可读 |
| `test_runtime.py::test_uploaded_attachment_survives_roundtrip` | `Service.upload` → 启动轮 + 周期轮 → `fetch` 字节一致 |
| `test_runtime.py::test_periodic_round_covers_all_tenants_when_unset` | 未配置 tenant_ids 时 tick>0 仍覆盖全部租户 |
| `test_attachment_repo.py::test_list_by_tenant_covers_all_statuses` | staged 与 committed 都在 known 内 |
| `test_http_api.py::test_media_bytes_match_recorded_checksum` | 读出字节 sha256 == metadata checksum，且 status 仍 staged |
| `test_http_api.py::test_media_size_mismatch_is_404` / `..._on_committed_marks_missing` | 字节被截断不服务；committed 行进入 missing |

## 未消除的窗口（记录，不隐瞒）

- `rename → metadata 提交` 的在途窗口仍在（单进程内极短），靠 `orphan_grace_seconds` 兜住；
  若未来 blob 与 metadata 跨机（对象存储），需要改为显式两阶段状态或写入前缀令牌。
- 全量 sha256 复算未放在读取热路径（与流式响应互斥），改由 size 一致性校验 + 契约用例承担；
  完整性巡检属 P3 备份恢复演练（C12 §8.6）的离线步骤。
- `commit_attachment`/`add_reference`/`remove_reference` 仍无生产调用方（C6 Non-Goal：协议帧 v0
  不带附件字段），故 30d「已引用」保留期在引用接入后才具备端到端可验收性。
