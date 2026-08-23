# Phase 1B 迁移工具与主数据源切换 — 设计

## Context

Storage Foundation（C1）已 merge（`0a83314d`）：`StorageRuntime.for_tenant(ctx)` → tenant-bound view（`infra/storage/runtime.py`），sync pool + bounded executor，`TenantProvisioningService` 幂等分区 provisioning（`infra/storage/provisioning.py`），分区稳定命名 `partition_name_for_tenant`（`infra/storage/partitioning.py`）。动机见 proposal.md - Why；行为契约见 specs/storage-migration/spec.md。

当前差距：`scripts/import_to_pg.py` 逐行 insert（经 `bootstrap.db` async repos），默认落 `default` tenant；迁移校验工具尚不存在（根目录 `verify_migration.py` 是检查 opencode.db 的无关脚本，不可复用）；生产仍 SQLite primary。`session/manager.py` 的 `control_store`（turn 持久化）显式收窄到 SQLite `SessionStore`，PG 后端抛 `RuntimeError` —— 这是 S2 必须定案的边界（见 D6）。

约束：M5-M7 在独立 branch/worktree 执行（Phase 1B = M5-M6）；main 基线全绿（2026-08-23 清理后 1031 passed / 0 failed），仍须保存可复现基线比较；文档语言中文；commit 无 Co-Authored-By、无 emoji。

## Goals / Non-Goals

**Goals:**
- 离线（S1 维护窗口）把 SQLite 数据批量、可恢复、幂等地导入 PG，并产出机器可读校验证据。
- 用显式 tenant mapping 约束导入，杜绝静默 `default`。
- 用可恢复、可审计的 S0-S4 状态机把 staging 切到 PG primary，并演练 PITR 恢复与回滚。
- 每个状态推进都有对账报告与证据入库。

**Non-Goals:**
- 不在 M6 实现 PG 版 turn control plane（turns 表 / 查询日志）——归属见 D6，完整上 PG 属 Phase 2（C2/C3）。
- 不做运行期 shadow-read 流量镜像（S1 只做快照导入后的离线校验，不做线上双读）。
- 不承诺 PG→SQLite 无损反向同步（S2 起禁止）；SQLite adapter 保留策略由产品模式决定，不随本 change 绑定（S4 原则）。
- 不做 5000 并发用户生产验收、autovacuum/REINDEX、真实 1024 维 recall 基线 —— 属 Phase 1C（M7）。

## Decisions

### D1 批量写入走独立 bulk seam，不经单连接 store RLock

现有两个 PG adapter 是单连接 + `threading.RLock`（`infra/storage/postgres_memory_store.py` / `postgres_session_store.py`），以逐方法粒度为业务读写作序列化。迁移在离线窗口进行，若逐条走 store 方法会：被 RLock 全程串行、批量吞吐低、且把导入路径与业务方法耦合。

**选择**：迁移工具持有自己的批量写入后端（独立 psycopg 连接 / COPY），按表分批 `COPY`：`memory_items` 是唯一按 tenant 分区的表，导入前需对目标 tenant 分区做幂等 provisioning（复用 `partition_name_for_tenant` + advisory-lock 双检 CREATE PARTITION，与 `TenantProvisioningService.provision_tenant` 同语义），保证 `COPY` 目标分区存在；`sessions`/`messages`/`memory_replacements` 是普通表（`tenant_id` 已包含在主键 `(tenant_id, key)` / `(tenant_id, id)` 内，非分区表），直接 `COPY` 到父表即可，无需 provisioning。

**备选**：复用 `PostgresMemoryStore` 的 `upsert_item` 等逐条方法 → 拒绝：RLock 全程串行 + 30k+ 行吞吐不可接受，且语义（content_hash 强化/upsert）会改写原值而非逐行复制。
**备选**：直接 `psycopg.copy` 到父表而不预建分区 → 拒绝：`memory_items` 无 DEFAULT 分区（隔离前提），缺分区时 COPY 失败，必须在导入前显式 provisioning。

### D2 checkpoint 与幂等重跑

迁移是长时间操作，需断点续传 + 幂等重跑。

**选择**：每轮运行写一个 checkpoint 文件（JSON，`<run-id>.json`，临时写 + rename 原子替换），记录每表已完成的高水位（按表主键边界，如 `memory_items` 按 id 区间、`messages` 按 `(session_key, seq)`、`sessions` 按 key）。恢复时跳过已完成批次。幂等由**保留源主键 + `ON CONFLICT DO NOTHING`**保证：迁移保留 SQLite 的原始主键（`memory_items.id`、`messages.id`、`sessions.key`），重跑不产生重复。

**备选**：按内容 hash/自然键去重（如 `content_hash`、`(session_key, seq)`）→ 拒绝：会与既有 upsert 语义纠缠，且无法保证逐行等价复制；保留主键 + 冲突忽略最直接、可审计。

### D3 显式 tenant mapping 强制

**选择**：迁移要求一个显式 mapping 文件（JSON），把「源身份（SQLite 库/workspace/通道）」映射到目标 `tenant_id`；未提供 mapping 或存在未映射源时，工具在 dry-run/预检阶段终止，不写任何数据。单用户 workspace 也必须由操作者显式指定目标 tenant（如 `programmatic:direct` / `cli:direct`），不默认 `default`。语义抽样校验按 tenant 维度执行，证明 mapping 生效且跨 tenant 不可见。

**备选**：自动派 tenant（按通道规则 `f"{channel}:{chat_id}"`）→ 拒绝：历史 SQLite 数据缺少可信通道身份字段，无法可靠自动派生；显式 mapping 是唯一可审计做法（延续决策 C「import 工具必须要求显式 tenant mapping」）。

### D4 机器可读校验

**选择**：校验工具输出 JSON 报告，含：逐表行数、关键字段 hash（按主键排序后对规范序列化字段做 hash，如 `md5`）、引用完整性（`messages.session_key` 命中 `sessions`；`memory_replacements` 外键命中）、语义抽样（随机样本的向量 top-k、关键词搜索命中集合、session `next_seq`）。任一维度失败 → 非零退出码 + 报告指出差异表/维度。报告写入 `openspec/evidence/phase1b/`（复用 phase1-storage evidence 目录模式），并在 change archive 时随 spec 归档。

**备选**：只比行数 → 拒绝：行数相等但内容漂移（顺序/字段值）会漏报；hash + 抽样是「机器可读校验」spec 要求的底线。

### D5 S0-S4 状态机：持久化 + 工具形态

**选择**：切换是一个 staging/运维工具（单写入者），状态持久化为 JSON 状态文件（与 checkpoint 同目录，原子写），字段含当前状态、每个状态的进入/退出时间、进入条件证据（校验报告引用）、退出条件证据（对账报告引用）。工具提供与状态推进一一对应的命令：`status`（读状态）、`import`（S1 导入）、`verify`（S1 校验）、`promote`（S1→S2）、`audit`（S2→S3 对账）、`rollback`（S1 内回滚）、`pitr-rehearsal`（演练）。状态推进仅当当前状态退出条件证据存在时允许；中断后从状态文件恢复。

**备选**：状态存 PG 控制表 → 拒绝：S0/S1 可能在任何 PG 数据之前运行，且状态应可在不连目标库时读取；文件形态可审计、可 diff、可 grep，足够支撑 staging 工具。

### D6 S2 边界：turn control plane 保持 SQLite-only（显式 dual-store）

`SessionManager.control_store`（`session/manager.py:346`）把 turn 持久化（create_turn/read_turn）收窄到 SQLite `SessionStore`，PG 后端抛 `RuntimeError`；`insert_query_log` 同理（`plugins/rachael/store.py`）。若不处理，PG-primary 模式下 agent turn 控制面直接不可用。

**选择**：M6 的 S2/S3 采用显式 dual-store：**PG 是用户数据（sessions/messages/memory_items/memory_replacements/插件数据）的 primary**；**turn control plane（turn 记录 + 查询日志）在审计窗口内继续落在 SQLite**，`SessionManager` 在 PG-primary 模式下持有一个 SQLite turns store（dual-store 配置，明确主/从角色，不把两个库都当 primary）。切换记录必须显式声明该边界与一致性/恢复/回滚策略。完整 turn control 上 PG 属 Phase 2（C2/C3 transactional outbox + turn admission）。

**备选**：在 M6 一并实现 PG turns 表 + turn 持久化 → 拒绝：会把「迁移正确性验证」与「新能力构建」混在同一个不可逆 cutover 点，放大风险面；SCALING_PLAN 第 8 节与 line 416 明确允许「纳入范围或记录策略」二选一，本设计选记录策略。
**一致性/恢复/回滚策略**：turn 记录是审计性、可由 PG 消息重建的派生态，不是用户数据的 source of truth；S2 期间 SQLite turns store 保留可读审计副本，回滚到 S1 时 SQLite 仍完整；S3 进入条件包含「turn 审计窗口覆盖至少一个业务周期」。

### D7 回滚边界

**选择**：S0/S1 可无损回 SQLite primary（回滚 = 保持 SQLite 为写路径，PG 仅 staging 副本）。S2 起无 PG→SQLite 反向同步，回滚**不是**切回 SQLite 数据，而是：应用仍连 PG；若必须回退应用版本，回退后仍指向 PG。回滚演练在 staging 完成并产出 runbook（含 PITR 恢复步骤与 RPO≤5min/RTO≤30min 记录）。schema 变更优先 forward-fix；破坏性变更按 expand/contract（决策延续 SCALING_PLAN §8）。

## Risks / Trade-offs

- [memory_items 分区缺失导致 COPY 失败] → 导入前按 mapping 全量幂等 provisioning；`partition_name_for_tenant` + advisory 锁双检（复用 M4H-4 已验路径）。
- [断点续传高水位记录与 ON CONFLICT 语义不一致 → 漏行/重行] → checkpoint 以主键边界记录；幂等由保留主键保证；校验维度含逐表行数 + 主键集合 hash 兜底。
- [turn control 留在 SQLite 被误解为「数据未迁移」] → 切换记录 + spec D6 场景显式声明；runbook 明确 primary 与审计副本角色。
- [S2 后无反向同步，误宣称可回切] → 工具在 S2 后禁止 rollback-to-SQLite 命令；文档/runbook 统一口径。
- [语义抽样受 embedding 维度/分布影响] → 抽样校验只比较源/目标在**同一查询**下的 top-k 命中与分数排序，不跨库比绝对值；向量差异详测留 Phase 1C。
- [main 测试基线变化，迁移工具测试混入既有失败] → 迁移测试独立目录 + 保存 main/分支可复现基线比较，基线以 2026-08-23 清理后（1031 passed / 0 failed）为准。

## Migration Plan

按 S0-S4 推进（staging）：

1. **S0 SQLite primary**：现状。准备 mapping 文件、staging PG（复用 `docker/debug/docker-compose.yml`）、记录 S0 证据。
2. **S1 snapshot import + shadow validation**：维护窗口对一致性快照运行 `import`（D1-D3）→ `verify`（D4）→ 校验报告入库。生产仍 SQLite。进入 S2 条件：全表行数/主键/hash/语义抽样通过 + turn control 边界声明。
3. **S2 PG primary + 审计窗口**：config `backend="postgres"`，新写入以 PG 为准；SQLite 仅审计副本（turn control 按 D6）；读取响应只取 PG。staging 全链路 smoke + `audit` 对账。回滚演练完成。
4. **S3 PG stable**：完成至少一个业务周期的对账、备份恢复、性能观察；停止 shadow，SQLite 归档只读快照。
5. **S4 旧实现清理**：仅在 S3 稳定 + SQLite 保留策略明确后删除迁移代码；SQLite adapter 保留由产品模式决定。

回滚：S0/S1 回 SQLite primary；S2 起回滚指应用仍连 PG（见 D7）。

## Open Questions

- S2 后生产环境的 turn 审计窗口具体覆盖多少个业务周期由运维在 staging 实测后定案（不影响 spec/任务拆分，仅影响 S3 进入条件的具体阈值）。
- staging 演练用的数据规模与真实 workspace 的差距（单用户 vs 多 tenant），影响演练脚本参数但不影响架构与任务拆分。
