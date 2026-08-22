# 存储层接口契约（M1）

> 来源：[phase1-storage.md](phase1-storage/phase1-storage.md) M1
> 用途：从 `MemoryStore2` / `SessionStore` 抽取的完整接口清单，作为 M2/M3
> `PostgresMemoryStore` / `PostgresSessionStore` 的等价实现契约。
> 状态：以 SQLite 实现（`memory2/store.py`、`session/store.py`）为基准。

## 1. 生命周期与线程模型

两个 store 均遵守以下约定，PG 后端必须一致：

- **构造**：`MemoryStore2(db_path, vec_dim=1024)` 打开连接并建 schema（幂等）。
  `SessionStore(db_path)` 同样。PG 后端构造时打开连接池 / 单连接（sync 后端），
  不立即执行迁移（schema 由 alembic / init 脚本负责）。
- **close()**：幂等，可重复调用；之后 `__del__` 也会调用一次。
  已 close 后再调用业务方法的行为：SQLite 会抛 `sqlite3.ProgrammingError`
  （connection is closed）。PG 后端应在已 close 后抛出等价异常（见第 3 节）。
- **线程安全**：SQLite 用 `threading.RLock()` 串行化所有操作；业务代码当前单进程
  单线程，但构造点会在多线程环境出现（dashboard + agent 共用）。PG 后端须用
  同一把锁包住 psycopg 连接 / 游标访问，保证等价并发语义。
- **schema 迁移**：SQLite 构造时 `executescript(SCHEMA)` 自建表（含 sqlite-vec
  向量表）。PG 后端不在此处建表，统一由 alembic 迁移管理。

## 2. 异常语义（两个后端必须一致）

- **不引入自定义异常类型**。SQLite 后端直接抛标准库异常，PG 后端抛出等价标准异常：
  - 数据库操作失败：SQLite 抛 `sqlite3.Error` 子类；PG 抛 `psycopg.Error` 子类。
    调用方只依赖「写操作失败即抛异常」这一事实，不 catch 具体类型。
  - 记录不存在（delete / update 场景）：返回 `False` / `None` / `0`，**不抛异常**
    （见各方法签名）。
  - `find_similar_items_for_dashboard(item_id)`：item 不存在抛 `KeyError(item_id)`；
    item 无 embedding 抛 `ValueError`。
- **返回值约定**（贯穿所有方法）：
  - `dict[str, object]` 表示「一行记录」，字段键见第 4 节；缺失即 `None`。
  - `tuple[list, int]` 表示「分页结果 + 总数」。
  - `int` 返回值（delete / count / record_replacements）表示受影响行数。
  - `str` 返回值（upsert_item 等）为状态标记，见各方法。

## 3. 关键差异点（PG 后端注意）

| 差异 | SQLite 现状 | PG 后端要求 |
|------|------------|-------------|
| close 后调用 | `sqlite3.ProgrammingError` | 抛 `psycopg.ProgrammingError`（或同义） |
| 原子自增 seq | `next_seq` 读 max(seq)+1 + `_ensure_next_seq_values`（非消费式） | 镜像 SQLite：返回 `max(stored next_seq, max(seq)+1)`，不消费（M3 定稿：`peek_next_message_id` 是无副作用 peek，消费式 SEQUENCE 会烧 seq；唯一性由 `insert_message` 原子 max 自增 + UNIQUE(tenant_id, session_key, seq) 保证，单连接 + RLock 无竞态） |
| 全文检索 | FTS5 trigram（`search_messages`） | `pg_trgm` GIN 索引 + ILIKE 子串匹配（M3），bm25 排序由「命中词数 DESC + seq DESC」近似 |
| 向量检索 | sqlite-vec（`vector_search` 等） | pgvector `<=>`，按 tenant 分区 + 每分区 HNSW（决策 B） |
| embedding 存储 | 独立 vec_items 表 + blob | `memory_items.embedding` 原生 `vector` 列（M2） |

## 4. MemoryStore2 接口清单（`memory2/store.py`，29 个 public 方法）

> 返回 dict 的字段键以 `memory_items` 表列为准：`id, memory_type, summary,
> content_hash, embedding, reinforcement, emotional_weight, extra_json, source_ref,
> happened_at, status, created_at, updated_at`；`scope_channel` / `scope_chat_id`
> 见列定义。embedding 对外暴露为 `list[float] | None`。

### 4.1 写操作

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `upsert_item` | `(memory_type, summary, embedding=None, *, source_ref=None, extra=None, happened_at=None, emotional_weight=0) -> str` | `str` | 写入或强化，返回 `"new:id"` / `"reinforced:id"`；content_hash 冲突即强化 |
| `upsert_consolidation_event` | `(*, source_ref, summary, embedding=None, extra=None, happened_at=None, emotional_weight=0) -> str` | `str` | 同一 source_ref 最多写一次，返回 `"new:id"` / `"skipped:empty"` / `"skipped:exists"` |
| `merge_item_raw` | `(item_id, new_summary, new_hash, new_embedding, new_extra=None) -> None` | `None` | 原子更新 merge 目标；content_hash 冲突时 supersede 旧条目 + 写新摘要 |
| `mark_superseded` | `(item_id) -> None` | `None` | 置 status='superseded' |
| `mark_superseded_batch` | `(ids) -> None` | `None` | 批量置 superseded |
| `undo_by_message_sources` | `(message_ids, *, dry_run=False) -> dict` | `dict` | 按 source_ref 命中的消息 id，批量 supersede 命中条目并恢复被其取代的旧条目（单事务）；返回 `{affected_ids, restored_ids, rollback_source_ids}`；`dry_run=True` 只预览不改写（M4，两个 store 各自实现） |
| `record_replacements` | `(*, old_items, new_item, source_ref=None, relation_type="supersede") -> int` | `int` | 写入 memory_replacements 关系行，返回写入数 |
| `reinforce_items_batch` | `(ids, emotional_weight=0) -> None` | `None` | 批量强化（reinforcement + 权重） |
| `delete_item` | `(item_id) -> bool` | `bool` | 不存在返回 False |
| `delete_items_batch` | `(ids) -> int` | `int` | 返回删除行数 |
| `delete_by_source_ref` | `(source_ref) -> int` | `int` | 返回删除行数 |

### 4.2 读操作

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `get_items_by_ids` | `(ids) -> list[dict]` | `list` | 保持入参顺序 |
| `list_replacements` | `() -> list[dict]` | `list` | 全量 replacement 关系 |
| `list_by_type` | `(memory_type) -> list[dict]` | `list` | 按类型过滤 |
| `list_events_by_time_range` | `(time_start, time_end, limit=200) -> list[dict]` | `list` | `memory_type='event'` + status active |
| `has_consolidation_source_ref` | `(source_ref) -> bool` | `bool` | |
| `has_item_by_source_ref` | `(source_ref, memory_type=None) -> bool` | `bool` | |
| `get_all_with_embedding` | `(include_superseded=False) -> list[_EmbeddingRow]` | `list` | `_EmbeddingRow` = 7 元组 `(id, memory_type, summary, embedding\|None, extra, source_ref, happened_at)`，用于迁移/双写 |
| `list_items_for_dashboard` | `(*, q="", memory_type="", status="", source_ref="", scope_channel="", scope_chat_id="", has_embedding=None, page=1, page_size=50, sort_by="created_at", sort_order="desc") -> tuple[list[dict], int]` | `tuple` | 分页 + 过滤 + 排序 |
| `get_item_for_dashboard` | `(item_id, *, include_embedding=False) -> dict\|None` | `dict\|None` | |
| `update_item_for_dashboard` | `(item_id, *, status=None, extra_json=None, source_ref=None, happened_at=None, emotional_weight=None) -> dict\|None` | `dict\|None` | 返回更新后的记录 |
| `find_similar_items_for_dashboard` | `(item_id, *, top_k=8, memory_type="", score_threshold=0.0, include_superseded=False) -> list[dict]` | `list` | item 不存在抛 `KeyError`；无 embedding 抛 `ValueError` |

### 4.3 检索

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `vector_search` | `(query_vec, top_k=8, memory_types=None, score_threshold=0.0, include_superseded=False, scope_channel=None, scope_chat_id=None, require_scope_match=False, hotness_alpha=0.0, hotness_half_life_days=14.0, time_start=None, time_end=None) -> list[dict]` | `list` | 每项带 `score`（余弦相似度）；支持热度和时间预过滤 |
| `vector_search_batch` | `(query_vecs, top_k=8, ...同 vector_search) -> list[list[dict]]` | `list` | 每个查询一组结果 |
| `find_similar_recent_events` | `(embedding, *, days_back=7, threshold=0.92, top_k=3) -> list[str]` | `list` | 返回 event id 列表 |
| `keyword_match_procedures` | `(action_tokens) -> list[dict]` | `list` | 对 trigger_tags 纯关键字匹配，只返回 `scope=tool_triggered` |
| `keyword_search_summary` | `(terms, memory_types=None, limit=20, time_start=None, time_end=None, scope_channel=None, scope_chat_id=None, require_scope_match=False) -> list[dict]` | `list` | OR-LIKE，带 `keyword_score`（命中词数/总词数），供 RRF 融合 |

### 4.4 私有方法（PG 后端可复用或改写，不作为对外契约）

`_migrate_existing_to_vec`、`_vec_insert`、`_vec_delete`、`_get_embedding_rows_by_time_filter`、
`_vector_search_vec`、`_vector_search_fullscan`、`_score_embedding_rows`。

## 5. SessionStore 接口清单（`session/store.py`，30 个 public 方法）

> 消息 dict 字段：`id, session_key, seq, role, content, ts, tool_chain, extra`。
> message_id 约定：`f"{session_key}:{seq}"`。session dict 字段：`key, created_at,
> updated_at, last_consolidated, metadata`（metadata 为 dict）。

### 5.1 会话生命周期

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `session_exists` | `(key) -> bool` | `bool` | |
| `upsert_session` | `(key, *, created_at, updated_at, last_consolidated, metadata) -> None` | `None` | INSERT ... ON CONFLICT(key) DO UPDATE |
| `create_session` | `(*, key, metadata=None, last_consolidated=0, last_user_at=None, last_proactive_at=None) -> dict` | `dict` | 返回新 session 记录；存在则 upsert |
| `update_session` | `(key, *, metadata=None, last_consolidated=None, last_user_at=None, last_proactive_at=None) -> dict\|None` | `dict\|None` | 更新并返回；不存在返回 None |
| `get_session_meta` | `(key) -> dict\|None` | `dict\|None` | 返回 metadata |
| `delete_session` | `(key, *, cascade=False) -> bool` | `bool` | cascade=True 时连带删消息 |
| `delete_sessions_batch` | `(keys, *, cascade=False) -> int` | `int` | 返回删除会话数 |
| `list_sessions` | `() -> list[dict]` | `list` | 全量 |
| `list_sessions_for_dashboard` | `(*, q="", channel="", updated_from="", updated_to="", has_proactive=None, page=1, page_size=50, sort_by="updated_at", sort_order="desc") -> tuple[list[dict], int]` | `tuple` | 分页 + 过滤 |
| `update_last_consolidated` | `(key, last_consolidated) -> None` | `None` | |

### 5.2 会话状态（presence / channel）

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `update_presence` | `(key, *, last_user_at=None, last_proactive_at=None) -> None` | `None` | 更新 presence 字段 |
| `get_presence` | `(key) -> dict[str, str\|None]\|None` | `dict\|None` | 返回 last_user_at / last_proactive_at |
| `list_presence` | `() -> dict[str, dict[str, str\|None]]` | `dict` | key -> presence |
| `most_recent_user_at` | `() -> str\|None` | `str\|None` | 全局最近 last_user_at |
| `get_channel_metadata` | `(channel) -> list[dict]` | `list` | 该 channel 的 session metadata 列表 |

### 5.3 消息

| 方法 | 签名 | 返回 | 语义要点 |
|------|------|------|---------|
| `count_messages` | `(session_key) -> int` | `int` | |
| `next_seq` | `(session_key) -> int` | `int` | 原子取下一 seq（PG 用 SEQUENCE/RETURNING） |
| `insert_message` | `(session_key, *, role, content, ts, seq, tool_chain=None, extra=None) -> dict` | `dict` | id = `session_key:seq`；返回完整记录 |
| `update_message` | `(message_id, *, role=None, content=None, tool_chain=None, extra=None, ts=None) -> dict\|None` | `dict\|None` | |
| `get_message` | `(message_id) -> dict\|None` | `dict\|None` | |
| `delete_message` | `(message_id) -> bool` | `bool` | |
| `delete_messages_batch` | `(ids) -> int` | `int` | |
| `fetch_session_messages` | `(session_key) -> list[dict]` | `list` | 按 seq 升序 |
| `fetch_by_ids` | `(ids) -> list[dict]` | `list` | |
| `fetch_by_ids_with_context` | `(ids, context) -> list[dict]` | `list` | 每条命中展开 ±context 行，dict 带 `in_source_ref: bool`；按 (session_key, seq) 排序 |
| `list_messages_for_dashboard` | `(*, session_key=None, q="", role="", page=1, page_size=25, sort_by="ts", sort_order="desc") -> tuple[list[dict], int]` | `tuple` | |
| `delete_session_messages_and_update_cursor` | `(session_key, *, ids, last_consolidated) -> int` | `int` | 删消息 + 更新 session.last_consolidated，返回删除数 |
| `search_messages` | `(query, *, session_key=None, role=None, limit=10, offset=0) -> tuple[list[dict], int]` | `tuple` | FTS5（PG 用 pg_trgm/tsvector，M3 专项） |

## 6. WIP bootstrap/db 对齐状态

继承自 `feature/pg-migration` 的 async repo 覆盖度：`AsyncMemoryRepository` 约 8/26、
`AsyncSessionRepository` 约 18/28 对齐本清单（缺口见
[phase1-storage.md](phase1-storage/phase1-storage.md) 2.3）。M2/M3 以本清单为准补齐。

## 7. 验收

- [ ] M2/M3 实现后，对每个 public 方法断言签名与本清单一致（pyright + 手测）。
- [ ] 同一组数据在 sqlite / postgres 后端下各方法返回一致（M7 专项）。
