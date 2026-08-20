# Phase 1 存储层改造计划（PostgreSQL + pgvector + Redis）

> 来源：[SCALING_PLAN.md](../../../SCALING_PLAN.md) Phase 1（P0 必须）
> 分支：`feature/scaling-phase1-storage`（继承 `feature/pg-migration` 的 WIP）
> 日期：2026-08-20

## 1. 背景与目标

当前 `memory2/store.py`（`MemoryStore2`）和 `session/store.py`（`SessionStore`）都基于**单文件 SQLite**：

- 写入全串行化，5000 用户并发写会排队等锁
- 向量检索依赖 `sqlite-vec`，数据全在内存、单核 QPS 有上限
- 无法水平扩展

Phase 1 的目标：

1. **后端可切换**：引入存储抽象，`sqlite`（单机兼容）与 `postgres` 双后端，业务代码经工厂获取 store
2. **PostgreSQL + pgvector**：迁移到异步连接池 + pgvector 向量检索，写入并发与检索可扩展
3. **Redis 缓存层**：缓存会话上下文、用户画像、向量检索结果，降低 DB 读压力
4. **渐进式迁移**：灰度 → 双写校验 → 全量切换，见 [migration_checklist.md](../../migration_checklist.md)

## 2. 现状盘点

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

### 2.2 业务代码现状（未迁移）

- `MemoryStore2`（1827 行，30+ 方法）——`sqlite3` + `threading.RLock`，含 `vector_search` / `vector_search_batch` / `merge_item_raw` / `find_similar_recent_events` / `keyword_search_summary` 等
- `SessionStore`（1035 行，40+ 方法）——`sqlite3` + FTS5，含 `next_seq` / `presence` / `search_messages` 等
- 直接构造点：`plugins/default_memory/engine.py`、`memory2/retriever.py`、`memory2/memorizer.py`、`bootstrap/tools.py`、`bootstrap/dashboard_api.py`、`bootstrap/init_workspace.py` 及多个测试

### 2.3 关键差距

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

**可复用**：PG schema（alembic 迁移）、repository 骨架（尤其 pgvector `<=>` 检索段）、import 脚本的表映射逻辑、`pg.py` 本地助手。

**需新建/补齐**：接口全量对齐、存储工厂与业务接线、Redis 缓存、`[storage]` / `[cache]` 配置、embedding 原生 `vector` 列 + HNSW 索引、依赖声明、import 批量 + 全表 + 校验。

一句话：WIP 是「并行新建 async 多租户数据层」的探索，业务代码一行都没接；计划（决策 A 路线 1）要求的是「现有 sync 接口不变、加 postgres 后端并真正切过去」。两者不冲突——计划就是围绕 WIP 缺口排的任务。

## 3. 核心架构决策（需确认）

### 决策 A：后端抽象策略（最重要）

- **路线 1（推荐，贴合 SCALING_PLAN 的 `create_store` 工厂）**：保持 `MemoryStore2` / `SessionStore` 的**同步接口不变**，新增 `PostgresMemoryStore` / `PostgresSessionStore` 实现同一套方法集；工厂按 `config.backend` 切换。业务改动面小、可灰度、现有测试可复用。
- **路线 2（WIP 方向，收益大改动大）**：以 `bootstrap/db` async repo 为准，把 memory2 / session / plugins / dashboard 全面改为 async + tenant 作用域，与 Phase 2「异步架构改造」合并推进。

建议：Phase 1 走**路线 1** 打通端到端并灰度；async 化整体挪到 Phase 2。

### 决策 B：向量检索形态（2026-08-20 已实证）

- Phase 1 用 **pgvector + HNSW**（SCALING_PLAN 3.1 选项 B），复用 WIP 检索逻辑；Qdrant（选项 A）留到 Phase 3
- **关键修正：必须按 `tenant_id` 原生 LIST 分区 + 每分区 HNSW 索引，不能用单全局 HNSW + 过滤**

**验证结论（`tests/`，15 万行 128 维聚类数据）：** 单全局 HNSW 索引 + `WHERE tenant_id=?` 过滤，recall@10（ef=40）随租户占比崩：50%→0.987、10%→0.463、2%→0.180、1%→0.133、0.1%→0.100；ef 提到 200 也救不回（1% 租户仅 0.257）。占比最小的租户（0.05%）planner 直接放弃索引改 seq scan——召回 1.000 但延迟 231ms vs 1.5ms（150k 行量级，生产 1M+ 更糟）。**对照实验**：同一份租户数据建独立索引，recall 回 0.99–1.00，证明退化是全局图遍历机制（过滤饿死遍历）而非数据稀疏。**缓解已用生产形态验证**：`CREATE TABLE ... PARTITION BY LIST (tenant_id)` + 父表建 HNSW → 每分区自动建索引，查询分区裁剪 + 单分区索引扫描（EXPLAIN 确认），recall 0.977–1.000、延迟 0.2–1.2ms 全量级达标。

- **前置工作**：schema 迁移，embedding 从 Text 改为原生 `vector(1024)` 列；`CREATE TABLE ... PARTITION BY LIST (tenant_id)` + `CREATE INDEX ... USING hnsw`（父表建，分区自动继承）
- 细节与复现见 [vector-validation.md](vector-validation.md)；脚本与结果在 [tests/](tests/README.md)，随文档入库

### 决策 C：多租户形态

WIP 全表带 `tenant_id`（单库多租户），与当前单用户 SQLite 不一致，也与「100 人 cell 模式（每用户一进程/一 workspace）」路线的关系需要明确：

- 若走**单库多租户**：`import_to_pg.py` 默认 `tenant="default"`，工厂按 session key 推导 tenant
- 若走 **cell 模式（每租户独立实例）**：Phase 1 的 PG 迁移是否仍是正确方向需重新评估（每实例继续用 SQLite 也成立）

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

### M4 工厂与接线
- [ ] `infra/storage/factory.py`：`create_store(config)` 按 backend 返回 store（沿用 SCALING_PLAN 设计）
- [ ] 改造构造点：`plugins/default_memory/engine.py`、`memory2/retriever.py`、`memory2/memorizer.py`、`bootstrap/tools.py`、`bootstrap/dashboard_api.py`、`bootstrap/init_workspace.py` 走工厂
- [ ] 测试构造点保持兼容（`MemoryStore2(path, vec_dim=...)` 直连仍可用）

### M5 Redis 缓存层
- [ ] `infra/cache/redis_cache.py`：`MemoryCache`（会话上下文 5min / 用户画像 1h / 检索结果 10min，对齐 SCALING_PLAN 1.2）
- [ ] 缓存失效策略：记忆 upsert / supersede 时按 session key 失效
- [ ] Redis 不可用时降级直连 DB（fail-open）

### M6 迁移脚本完善
- [ ] `import_to_pg.py` 批量 insert（batch_size=1000），补齐所有表与 `memory_replacements` / rachael 数据
- [ ] `verify_migration.py`：逐表行数 + 抽样 hash 对比
- [ ] 双写验证：新写入同时落 SQLite + PG，定期比对

### M7 验证
- [ ] 现有 SQLite 单测全绿（不回归）
- [ ] 新增：同一组数据在 sqlite / postgres 后端下返回一致（含向量检索 top-k）
- [ ] 性能基线：`memory2` 单测 + 压测脚本对比 SQLite vs PG（写吞吐、检索 P95）
- [ ] 端到端：`backend="postgres"` 下对话 / 记忆检索 / dashboard / session 全链路跑通

## 5. 验收标准

- [ ] `config.toml` 切 `backend = "postgres"` 后全链路可用，切回 `sqlite` 无回归
- [ ] `import_to_pg.py` 将现有 workspace 数据完整迁入，`verify_migration.py` 通过
- [ ] `MemoryStore2` / `SessionStore` 全方法在 postgres 后端有等价实现
- [ ] embedding 为原生 `vector` 列，`memory_item` 按 tenant 分区 + 每分区 HNSW 索引，检索不再走 `::vector` 转换
- [ ] 向量召回验证：真实数据下任意租户占比 recall@10 ≥ 0.9（ef=40，对齐 [vector-validation.md](vector-validation.md) 分区方案实测）
- [ ] Redis 命中后读延迟下降，Redis 挂掉服务不受影响
- [ ] 写吞吐相对 SQLite 有量级提升（对齐 SCALING_PLAN 表：10x 写入吞吐）

## 6. 风险与开放问题

| 风险/问题 | 影响 | 对策 |
|-----------|------|------|
| async repo 与 sync 接口双轨维护 | 逻辑分散 | 路线 1 只把 `bootstrap/db` 当作 sync 后端内部实现，不暴露 async 接口给业务 |
| 向量检索无索引（Text 列 + 转换） | 大库退化为全扫 | M2 必须做原生 vector 列 + HNSW |
| **全局 HNSW + 租户过滤召回坍缩**（已实测：占比 ≤10% 时 recall@10 从 0.99 崩到 0.46→0.10；最小租户改 seq scan 延迟 x150） | 小租户检索劣化 | **按 tenant_id LIST 分区 + 每分区 HNSW 索引**（实测 recall 0.977–1.000、0.2–1.2ms），见决策 B 与验证文档 |
| FTS5 → PG 全文检索语义差异 | 中文分词结果不同 | M2/M3 明确 `pg_trgm` vs tsvector 取舍并加回归测试 |
| 多租户形态未定（决策 C） | 影响 schema 与 import | 需要用户拍板后再定 tenant 策略；分区方案本身兼容「单库多租户」 |
| import 逐行写入 | 万级数据慢 | M6 批量 + 进度日志 |

## 7. 与后续 Phase 的关系

- **Phase 2**：async 化（AgentLoop / LLM 客户端 / 存储全部异步）——依赖决策 A 的路线选择
- **Phase 3**：独立向量库 Qdrant——依赖决策 B 的 pgvector 阶段成果
- **Phase 4**：消息队列 + Worker 池——依赖 Phase 2 async + Redis 就绪
