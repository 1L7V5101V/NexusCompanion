# Phase 1 存储层改造计划（PostgreSQL + pgvector）

> 来源：[SCALING_PLAN.md](../../scaling/SCALING_PLAN.md) Phase 1（P0 必须）
> 分支：`feature/scaling-phase1-storage`（继承 `feature/pg-migration` 的 WIP）
> 初版日期：2026-08-20
> 架构复审：2026-08-21
> 当前状态：M0-M4 功能实现完成；M4.5 架构硬化与 merge gate 进行中；不得进入旧版 Redis M5
> 执行交接：[m4.5-architecture-hardening.md](m4.5-architecture-hardening.md)

## 1. 背景与目标

当前 `memory2/store.py`（`MemoryStore2`）和 `session/store.py`（`SessionStore`）以 SQLite adapter 为单机基线。Phase 1 已增加 PostgreSQL adapter，但“adapter 可运行”不等于“可安全承载多租户和多 Worker”。

Phase 1 的目标调整为：

1. **稳定 storage interface**：SQLite 与 PostgreSQL 作为两个 adapter，业务代码只依赖小而明确的 interface，不传播具体实现联合类型。
2. **PostgreSQL + pgvector**：使用原生 vector、tenant-aware 检索和可运维索引形态，保留 SQLite 兼容路径。
3. **可信多租户隔离**：tenant 从服务端可信 identity 派生并贯穿 session、memory、dashboard、proactive 和附件 metadata。
4. **安全的连接与阻塞模型**：真实使用进程级 pool，并确保同步 DB 调用不会直接阻塞 event loop。
5. **渐进式迁移**：按主计划 S0-S4 状态机执行导入、校验、主数据源切换和回滚演练。
6. **缓存后置**：Redis cache 只有在 PostgreSQL 基准证明重复读是瓶颈且失效模型可靠时才启用，不是 Phase 1 merge 前置。

## 2. M0 前基线盘点（历史）

### 2.1 分支已有 WIP（继承自 `feature/pg-migration`，2026-07-14）

| 模块 | 内容 | 状态 |
|------|------|------|
| `bootstrap/db/config.py` | `DatabaseConfig`（url/pool_size/max_overflow） | 可用 |
| `bootstrap/db/engine.py` | async engine + session factory 工厂 | 可用 |
| `bootstrap/db/models/` | SQLAlchemy ORM：memory / session / proactive / rachael / tenant / extras，`Base` + `TenantMixin` + `TimestampMixin` | 可用 |
| `bootstrap/db/repository/` | `AsyncMemoryRepository`（pgvector `<=>` 检索 + Python 兜底）、`AsyncSessionRepository`、`AsyncProactiveRepository` | 部分覆盖 |
| `alembic/` | 初始迁移 + rachael/extras 迁移 | 可用 |
| `scripts/import_to_pg.py` | SQLite → PG 导入（sessions / memory / proactive / JSON configs） | 逐行 insert，待优化 |
| `pg.py` | 本地 PG 启停/psql 助手（Windows） | 工具 |
| `verify_migration.py` | 迁移校验 | 待完善 |

### 2.2 M0 前业务代码状态

- `MemoryStore2`（1827 行，30+ 方法）——`sqlite3` + `threading.RLock`，含 `vector_search` / `vector_search_batch` / `merge_item_raw` / `find_similar_recent_events` / `keyword_search_summary` 等
- `SessionStore`（1035 行，40+ 方法）——`sqlite3` + FTS5，含 `next_seq` / `presence` / `search_messages` 等
- 直接构造点：`plugins/default_memory/engine.py`、`memory2/retriever.py`、`memory2/memorizer.py`、`bootstrap/tools.py`、`bootstrap/dashboard_api.py`、`bootstrap/init_workspace.py` 及多个测试

### 2.3 M0 前关键差距

| 差距 | 说明 |
|------|------|
| **接口不一致** | WIP repo 是 async + `tenant_id` 作用域；`AsyncMemoryRepository` 约 **8/26** 个方法对齐 `MemoryStore2`，`AsyncSessionRepository` 约 **18/28** 个对齐 `SessionStore`。业务代码是 sync 单用户。**不可直接替换** |
| **embedding 是 Text 列** | `MemoryItemModel.embedding = Text`，查询时 `::vector(:dim)` 转换 → **无法建 HNSW/IVFFlat 索引**，大库检索退化 |
| **依赖未声明** | `asyncpg` / `psycopg` / `redis` 不在 `requirements.txt` / `pyproject.toml` |
| **无配置入口** | `agent/config_models.py` 是 dataclass 结构，尚无 `[storage]` / `[cache]` 段 |
| **迁移脚本逐行 insert** | `import_to_pg.py` 逐行 await，万级数据慢 |
| **FTS 语义差异** | SQLite FTS5 中文分词与 PG `pg_trgm` / tsvector 语义不同，`search_messages` 需专门适配 |

**WIP 缺失的关键方法（M2/M3 需补齐）**：

- `MemoryStore2` 侧缺：`get_all_with_embedding`、`vector_search_batch`、`merge_item_raw`、`find_similar_recent_events`、`keyword_search_summary`、`keyword_match_procedures`、`mark_superseded(_batch)`、`record_replacements`、`list_replacements`、`reinforce_items_batch`、`get_items_by_ids`、`has_consolidation_source_ref`、`get_item_for_dashboard`、`update_item_for_dashboard`、`find_similar_items_for_dashboard`、`delete_by_source_ref`、`has_item_by_source_ref`
- `SessionStore` 侧缺：`list_presence`、`fetch_by_ids_with_context`、`list_messages_for_dashboard`、`delete_session_messages_and_update_cursor`、`get_session_meta`、`most_recent_user_at`、`update_last_consolidated`、`get_channel_metadata`、`delete_messages_batch`、`fetch_by_ids`

### 2.4 结论：WIP 可复用点 vs 需新建

**已复用并完成**：PG schema、pgvector 检索骨架、import 表映射、`pg.py`、依赖与配置、SQLite/PG factory、Memory/Session PostgreSQL adapter。

**当前需补齐**：storage interface、`TenantContext` / `TenantResolver`、真实连接池与 event-loop 隔离、tenant isolation 测试、生产分区 provisioning、迁移工具和完整 CI gate。

一句话：M0-M4 已证明双 adapter 的功能可行性；M4.5 必须把调用方 interface、tenant、连接生命周期和验证证据收口，才能进入迁移与切换。

## 3. 架构复审后的核心决策

### 决策 A：storage interface 与阻塞模型

- 保留 SQLite/PG 双 adapter，但调用方必须依赖 Protocol 或等价 storage interface，不能继续依赖 `MemoryStore2 | PostgresMemoryStore`、`SessionStore | PostgresSessionStore` 联合类型。
- `MemoryStore2` / `SessionStore` 是 SQLite adapter，不再同时充当 interface 定义。
- Phase 1 不要求一次重写全部调用链为 async；但 merge 前必须二选一并记录决策：
  1. async adapter + 进程级 async pool；或
  2. sync pool + 有界 executor，把同步 DB 调用移出 event loop。
- factory/runtime 负责 adapter、pool 和 shutdown 生命周期；调用方不得按 tenant 创建长期 connection/store。

### 决策 B：向量检索形态（2026-08-20 已实证）

- Phase 1 使用 **pgvector + HNSW**；只有真实容量、运维或检索需求证明 PostgreSQL 无法满足时，才通过既有 storage seam 评估独立向量系统。
- **关键修正：必须按 `tenant_id` 原生 LIST 分区 + 每分区 HNSW 索引，不能用单全局 HNSW + 过滤**

**验证结论（`tests/`，15 万行 128 维聚类数据）：** 单全局 HNSW 索引 + `WHERE tenant_id=?` 过滤，recall@10（ef=40）随租户占比崩：50%→0.987、10%→0.463、2%→0.180、1%→0.133、0.1%→0.100；ef 提到 200 也救不回（1% 租户仅 0.257）。占比最小的租户（0.05%）planner 直接放弃索引改 seq scan——召回 1.000 但延迟 231ms vs 1.5ms（150k 行量级，生产 1M+ 更糟）。**对照实验**：同一份租户数据建独立索引，recall 回 0.99–1.00，证明退化是全局图遍历机制（过滤饿死遍历）而非数据稀疏。**缓解已用生产形态验证**：`CREATE TABLE ... PARTITION BY LIST (tenant_id)` + 父表建 HNSW → 每分区自动建索引，查询分区裁剪 + 单分区索引扫描（EXPLAIN 确认），recall 0.977–1.000、延迟 0.2–1.2ms 全量级达标。

- **前置工作**：schema 迁移，embedding 从 Text 改为原生 `vector(1024)` 列；`CREATE TABLE ... PARTITION BY LIST (tenant_id)` + `CREATE INDEX ... USING hnsw`（父表建，分区自动继承）
- 细节与复现见 [vector-validation.md](vector-validation.md)；脚本与结果在 [tests/](tests/README.md)，随文档入库

### 决策 C：多租户形态

5000 用户目标按**共享 PostgreSQL、单库多租户**设计；cell 模式保留为后续部署策略，不作为当前绕过 tenant isolation 的理由。

- tenant 必须由 `TenantResolver` 从可信 channel/auth identity 派生，再通过 `TenantContext` 传给 storage runtime。
- session key 不是 tenant identity；不能把解析 session key 当作授权边界。
- 多用户入口不得隐式回退到 `tenant_id="default"`。
- 单用户 SQLite 兼容路径如保留默认 tenant，必须与多用户入口显式隔离并有测试。
- import 工具必须要求显式 tenant mapping；不得把全部历史数据无提示导入 `default`。

## 4. 任务拆解

### M0 依赖与配置
- [x] 本地开发 PG：`docker/debug/docker-compose.yml` 加 `postgres` 服务（`pgvector/pgvector:pg17` 镜像，建库 + `CREATE EXTENSION vector`，数据卷持久化）——WSL/Docker 已可用（2026-08-20），优先 Docker；原生 PG 17.10（含验证用的 `vecbench` 库）保留作对照
- [x] `requirements.txt` / `pyproject.toml` 补充：`asyncpg`、`psycopg[binary]`（sync 后端用）、`redis`（asyncio）、`pgvector`
- [x] `agent/config_models.py` 新增 `StorageConfig`（backend / postgres_url / pool_size）与 `CacheConfig`（redis_url / ttl 策略）
- [x] `config.example.toml` 新增 `[storage]` / `[cache]` 段（对齐 SCALING_PLAN 配置示例）
- [x] `pg.py` 增加 `init-db`（建库 + `CREATE EXTENSION vector` + 建用户）子命令（原生路径备用）

### M1 接口界定
- [x] 从 `MemoryStore2` / `SessionStore` 抽出完整接口清单（方法签名 + 返回类型），作为 [storage-interface.md](../storage-interface.md) 存底
- [x] 明确 `close()` / 生命周期 / 异常语义在两个后端一致

### M2 PostgresMemoryStore（sync 后端）
- [x] 用 psycopg（sync）实现 `MemoryStore2` 全部 28 个 public 接口，落 `infra/storage/postgres_memory_store.py`（`PostgresMemoryStore`，单连接 + `threading.RLock`，`register_vector` 自动转换 list↔vector，所有方法带 tenant scope）；通过 `tmp/verify_pg_memory.py` 15 项功能验证
- [x] schema：embedding 改原生 `vector(1024)` 列；`memory_item` 按 `tenant_id` LIST 分区，父表建 HNSW 索引（每分区自动继承），见 `alembic/versions/a3d5c7e9f1b2_partition_memory_items.py`（含 `consolidation_events` 复合 PK、`memory_replacements.source_ref` 补列）
- [x] 补齐 WIP 缺失方法：`get_all_with_embedding` / `vector_search_batch` / `merge_item_raw` / `find_similar_recent_events` / `keyword_match_procedures` / `keyword_search_summary` / `mark_superseded(_batch)` / `record_replacements` / `reinforce_items_batch` 等
- [x] 关键词检索：SQLite 基线 `keyword_search_summary` 本就是 OR-LIKE 子串匹配（`store.py` 未用 FTS5 MATCH），PG 镜像为 `summary LIKE %s` 逐项对齐，无需 `pg_trgm`/tsvector；中文子串匹配由 UTF-8 LIKE 直接保留
- [x] 两后端 parity：`tmp/parity_smoke.py` 同数据写 SQLite + PG，向量 top-k 排序 / score、scope 过滤、关键词、替换、事件检索一致（`score=0` 精确平局时 HNSW 与 KNN 截断顺序可不同，测试只比较明确项与分数）

### M3 PostgresSessionStore（sync 后端）
- [x] 实现 `SessionStore` 全部接口，落 `infra/storage/postgres_session_store.py`（28 个 public 方法 + close/__del__，单连接 + RLock，全部按 tenant_id 作用域）；`tmp/verify_pg_session.py` 13 组验证全过
- [x] `next_seq` 定稿为非消费式：镜像 SQLite 返回 `max(stored, max(seq)+1)`，不采用 SEQUENCE 消费式（`peek_next_message_id` 无副作用 peek，消费式会烧 seq；原子性由 `insert_message` max 自增 + UNIQUE 保证），见 [storage-interface.md](../storage-interface.md) 3 节
- [x] `search_messages` 用 `pg_trgm`（迁移 b6e9d2c4a8f1 建 GIN 索引加速 ILIKE 子串匹配），对标 SQLite FTS5 trigram；bm25 排序由「命中词数 DESC + seq DESC」近似
- [x] 迁移 b6e9d2c4a8f1：sessions/messages 主键改 `(tenant_id, key)` / `(tenant_id, id)`（决策 C 跨 tenant 不撞 key）、messages UNIQUE(tenant_id, session_key, seq)、messages.id 放宽到 511、pg_trgm 扩展 + content GIN 索引
- [x] 两后端 parity：`tmp/parity_session_smoke.py` 同数据写 SQLite + PG，next_seq 序列、session/message 结构、presence、dashboard 分页、search 命中集合、delete 语义一致（自动时间戳只比格式）
- [x] 补齐 WIP `session_repo` 缺失方法——按决策 A（路线 1，sync 后端为准）此项关闭：async `session_repo` 无生产引用，整体挪 Phase 2；SessionStore 侧 2.3 列出的 10 个缺失方法已由本 M3 全部实现

### M4 工厂与接线（功能完成，架构 gate 未完成）
- [x] `infra/storage/factory.py`：`create_store` / `create_session_store` 按 `config.backend` 返回 store（`StorageConfig` 无 sqlite_path，sqlite 路径由调用方显式传入）；postgres 分支构造前 `to_regclass` 探测 schema，缺失抛含 `alembic upgrade head` 的 `RuntimeError`
- [x] 改造构造点：`plugins/default_memory/engine.py`、`memory2/memorizer.py`、`bootstrap/tools.py`、`bootstrap/dashboard_api.py`、`bootstrap/init_workspace.py` 走工厂（`memory2/retriever.py` 经 engine 注入 store，无直接构造）
- [x] 测试构造点保持兼容（`MemoryStore2(path, vec_dim=...)` 直连仍可用）
- [ ] M4.5 架构硬化与 merge gate 完成，见 [m4.5-architecture-hardening.md](m4.5-architecture-hardening.md)

### M4.5 架构硬化与 merge gate（当前阶段）
- [ ] 定义 storage Protocol/interface，让 SQLite/PG 成为两个 adapter；调用方不再传播具体实现联合类型
- [ ] 引入 `TenantContext` / `TenantResolver`，从可信 inbound identity 贯穿 session、memory、dashboard、proactive、undo/source_ref 和附件 metadata
- [ ] 移除多用户路径隐式 `tenant_id="default"`，增加 A/B tenant 越权测试
- [ ] 定型 async pool 或 sync pool + bounded executor，确保 DB 调用不直接阻塞 event loop
- [ ] `pool_size` 控制真实 adapter；transaction recovery、pool exhaustion 和 shutdown 有测试
- [ ] 首次用户写入不执行动态分区 DDL，改为幂等 tenant provisioning
- [ ] Phase 1 storage 范围 pyright error 清零，全量测试相对 main 基线无回归
- [ ] 审查并处理 `6d09bc52` 删除的 15 个非存储测试文件；不得以删除测试替代覆盖迁移

### M5 迁移工具与校验
- [ ] `import_to_pg.py` 使用批量 COPY/批量 insert，支持可配置 batch、进度、断点续传和幂等重跑
- [ ] 补齐所有 Phase 1 目标表，包括 `memory_replacements`、session、memory 和明确纳入本阶段的插件数据
- [ ] `verify_migration.py` 提供逐表行数、关键字段 hash、引用完整性和语义抽样
- [ ] tenant mapping 必须显式；禁止无提示把所有数据导入 `default`
- [ ] 导入与校验结果可机器读取并保存为迁移证据

### M6 主数据源切换
- [ ] 按主计划 S0-S4 状态机执行：SQLite primary → shadow import → PostgreSQL shadow write/read verify → PostgreSQL primary → SQLite retirement
- [ ] 每个状态明确 primary、允许写路径、对账方式、进入条件和退出条件
- [ ] 不采用无主次的简单双写；写 PostgreSQL 后没有反向同步时，不承诺无损回切 SQLite
- [ ] 完成 staging cutover、PITR 恢复和回滚演练

### M7 生产基准与对等
- [ ] SQLite/PG 共用契约测试，向量 top-k、关键词、session、dashboard、undo/source_ref 语义达到记录的允许差异
- [ ] 双后端端到端等价性覆盖完整 PassiveTurn，而非仅 store 层断言
- [ ] 真实 1024 维 embedding 和真实 tenant 分布下 recall@10、检索 P95 达标
- [ ] 5000 tenant 下验证 planning latency、catalog、migration、backup/restore、autovacuum 和 REINDEX
- [ ] `EXPLAIN` 确认 tenant 分区裁剪；跨 tenant 查询返回空且无越权
- [ ] 连接池并发、event-loop lag、transaction recovery 和 shutdown 达标
- [ ] BM25/关键词搜索对等测试明确允许的排序差异；BM25 implementation 可独立提交，但差异必须可观测

### Cache 条件任务（不阻塞 Phase 1）
- [ ] 只有 PostgreSQL 基线证明重复读是瓶颈，并能定义可靠失效与版本化 key 时，才设计 `MemoryCache`
- [ ] Cache 必须 fail-open，且 Redis 故障不影响数据库正确性
- [ ] 不缓存尚无稳定 tenant/version key 的 session、profile 或检索结果

## 5. 验收标准

- [ ] `config.toml` 切 `backend = "postgres"` 后 staging 全链路可用，SQLite adapter 契约测试无回归
- [ ] tenant 从可信请求上下文派生，所有存储入口有 A/B tenant isolation 证据
- [ ] storage interface 隐藏 adapter 差异，调用方不依赖具体实现联合类型
- [ ] DB 调用不直接阻塞 event loop，连接池配置、预算、指标和 shutdown 生效
- [ ] `MemoryStore2` / `SessionStore` 的业务语义在 PostgreSQL adapter 下有记录的等价实现或明确差异
- [ ] embedding 为原生 `vector` 列；分区裁剪和 HNSW 在真实数据与 5000 tenant 运维基准下达标
- [ ] `import_to_pg.py` 与 `verify_migration.py` 支持可恢复导入和机器可读校验
- [ ] staging PostgreSQL primary、对账、PITR 恢复和回滚演练完成
- [ ] Phase 1 修改范围 pyright 通过；全量测试相对 main 基线无回归且未以删除测试换取绿色
- [ ] 性能和成本结论来自可重复基准，不使用固定“10x”或固定实例数作为验收值

## 6. 风险与开放问题

| 风险/问题 | 影响 | 对策 |
|-----------|------|------|
| 所有 PG 数据落入 `default` tenant | P0 越权与数据混合 | `TenantResolver` + request-scoped `TenantContext` + 全入口隔离测试 |
| sync 单连接 + `RLock` | P0 event-loop 阻塞和全局串行 | async pool 或 sync pool + bounded executor；显式 lifecycle |
| 具体实现联合类型扩散 | 调用方与 adapter 强耦合 | 建立 storage interface seam，共用契约测试 |
| 首次请求动态 CREATE PARTITION | 用户延迟、DDL race、catalog 风险 | provisioning control path + 幂等锁 + 5000 tenant 基准 |
| 全局 HNSW + tenant filter 召回坍缩 | 小租户检索劣化 | 当前使用 tenant LIST 分区；生产基准不通过时评估 hash bucket/冷热分层 |
| FTS5 与 PG 搜索排序差异 | 召回和排序漂移 | BM25/关键词对等测试，记录允许差异后再 cutover |
| 无主次双写或错误回滚承诺 | PostgreSQL primary 后回切丢数据 | 使用 S0-S4 状态机，明确 primary 与不可逆点 |
| 删除非存储测试以换取 CI | 覆盖回退、隐藏回归 | 拆分 orphan test 清理，恢复或提供等价覆盖 |
| Redis cache 提前加入 | 脏读与失效复杂度 | 等 PG 基线证明收益后再启用 |

## 7. 与后续 Phase 的关系

- **Phase 0 可观测性与负载工具**可在低冲突分支并行，为 M7 提供统一证据。
- **Phase 2 单进程并发与阻塞隔离**依赖 Phase 1 storage interface 和阻塞模型稳定；不能在 sync 单连接上直接放开 turn 并发。
- **Phase 3 durable ingress/outbox**依赖 tenant-aware event envelope 和工具幂等语义，不以 Redis cache 为前置。
- **独立向量系统**不是预定 Phase；只有 pgvector 真实基准触发时才通过 storage seam 评估。

## 8. 版本与工作树管理

- 唯一工作目录：`D:\.Projects\NexusCompanion\.claude\worktrees\scaling-phase1-storage`。
- 唯一工作分支：`feature/scaling-phase1-storage`；不得在主工作树直接实现 Phase 1。
- 开始和提交前运行 `git status --short --branch`；工作树不干净时先理解现有改动，不覆盖用户工作。
- 不修改、清理或删除其他 worktree；不使用 `git reset --hard`、`git clean`、force push。
- 保留已有 M0-M4 commit 历史，不通过 rebase/amend 重写；同步 main 时使用显式 merge，并在合并前保持工作树干净。
- 每个 commit 只包含一个可验证 concern；无关 main 债务单独提交或留在 main 修复。
- checklist 只在测试、日志或结果文件可复现时勾选；分支存在或代码已写不等于 milestone 完成。
- commit 不添加 `Co-Authored-By`。
