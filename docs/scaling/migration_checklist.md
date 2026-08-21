# 迁移执行清单（5000用户扩展）

> 文档状态：历史执行清单，阶段编号和部分前置假设尚未同步 2026-08-21 的架构复审。
> 实施前应先按 [`SCALING_PLAN.md`](./SCALING_PLAN.md) 的 Phase 0-6、exit gate 与 S0-S4 迁移状态机重切任务；发生冲突时，以主计划和已验证代码为准。
> **向量方案已定**：pgvector + 按 `tenant_id` 分区 HNSW，不引入独立向量数据库。
> 依据 `docs/tasks/phase1-storage/vector-validation.md`。
>
> **可与阶段 1 并行的工作**（互不冲突，也不碰阶段 1 的文件）：
>
> - 监控埋点（阶段 5.0）—— 阶段 1 的双写一致性告警依赖它，属前置而非收尾
> - webchat 鉴权（阶段 4.5.2 的鉴权部分）—— `tenant_id` 已在阶段 1 schema 就位
> - webchat 前端（阶段 4.5.2 的前端部分）—— 纯前端资产
>
> **必须串行**：阶段 2 异步化（与阶段 1 抢 store 调用点，且在 SQLite 上做 asyncio
> 会写一堆 `run_in_executor` 包装，阶段 1 落地后全部拆掉）。

## 阶段 0：准备工作（Week 0）

### 0.1 环境准备

- [ ] **开发环境**
  
  - [ ] 安装 PostgreSQL 15+ 
  - [ ] 安装 Redis 7+
  - [ ] 安装 Docker Compose（本地测试）
  - [ ] 配置 pgvector 扩展：`CREATE EXTENSION vector;`

- [ ] **依赖更新**
  
  ```bash
  # requirements.txt 新增
  asyncpg>=0.29.0
  redis[asyncio]>=5.0.0
  httpx>=0.27.0
  prometheus-client>=0.19.0
  ```

- [ ] **监控工具**（Week 1-2 并行，双写验证的前置）

  - [ ] Grafana + Prometheus 搭建
  - [ ] 日志聚合（ELK 或 Loki）
  - [ ] `infra/monitoring/metrics.py` 最小指标集 —— 代码库当前无任何
        prometheus 埋点，需从零建：

    ```python
    dual_write_mismatch = Counter("dual_write_mismatch_total", "...", ["table"])
    dual_write_total    = Counter("dual_write_total", "...", ["table"])
    store_latency       = Histogram("store_op_seconds", "...", ["backend", "op"])
    vector_recall       = Gauge("vector_search_recall", "...", ["tenant_bucket"])
    ```

  - [ ] 告警：`dual_write_mismatch / dual_write_total > 0.1%`
  - [ ] 告警：`store_op_seconds{backend="postgres"}` P99 > 100ms
  - [ ] 告警：`vector_search_recall < 0.9`

###  0.2 基线测试

- [ ] 记录当前性能指标
  
  - [ ] 消息处理平均耗时：_______ 秒
  - [ ] 记忆检索 P95 延迟：_______ ms
  - [ ] 数据库文件大小：_______ MB
  - [ ] 当前用户数：_______

- [ ] 性能瓶颈定位
  
  ```bash
  # SQLite 慢查询分析
  sqlite3 ~/.nexus/workspace/memory/store.db
  > PRAGMA analysis_limit=1000;
  > ANALYZE;
  ```

---

## 阶段 1：存储层迁移（Week 1-4）

### 1.1 PostgreSQL 搭建（Week 1）

- [ ] **数据库初始化**
  
  ```sql
  -- 创建数据库
  CREATE DATABASE nexus_production;
  
  -- 创建用户
  CREATE USER nexus_app WITH PASSWORD 'strong_password';
  GRANT ALL PRIVILEGES ON DATABASE nexus_production TO nexus_app;
  
  -- 启用扩展
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- 全文检索
  ```

- [ ] **表结构迁移**
  
  ```bash
  # 生成迁移脚本
  python scripts/generate_pg_schema.py
  
  # 应用到数据库
  psql -U nexus_app -d nexus_production -f migrations/001_initial_schema.sql
  ```

- [ ] **创建索引**
  
  ```sql
  -- 向量索引（HNSW）
  CREATE INDEX ON memory_items USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
  
  -- 会话查询索引
  CREATE INDEX idx_memory_session ON memory_items(session_key, status);
  CREATE INDEX idx_memory_created ON memory_items(created_at DESC);
  
  -- 全文检索索引
  CREATE INDEX idx_memory_summary_trgm ON memory_items USING gin (summary gin_trgm_ops);
  ```

- [ ] **配置连接池**
  
  ```toml
  # config.toml
  [storage.postgres]
  host = "localhost"
  port = 5432
  database = "nexus_production"
  user = "nexus_app"
  password = "${POSTGRES_PASSWORD}"
  pool_min_size = 10
  pool_max_size = 50
  command_timeout = 60
  ```

### 1.2 Redis 缓存层（Week 1）

- [ ] **Redis 安装配置**
  
  ```bash
  # redis.conf 调优
  maxmemory 8gb
  maxmemory-policy allkeys-lru
  save ""  # 禁用 RDB，避免阻塞
  appendonly yes
  appendfsync everysec
  ```

- [ ] **缓存策略实现**
  
  ```python
  # infra/cache/redis_cache.py
  class MemoryCache:
      async def get_session_context(self, session_key):
          # L1: Redis 缓存（5分钟）
          cached = await self.redis.get(f"ctx:{session_key}")
          if cached:
              return json.loads(cached)
  
          # L2: 数据库查询
          items = await self.db.fetch_recent(session_key, limit=20)
          await self.redis.setex(f"ctx:{session_key}", 300, json.dumps(items))
          return items
  ```

- [ ] **缓存预热脚本**
  
  ```bash
  # 启动时预加载热点用户
  python scripts/warmup_cache.py --top-users 100
  ```

### 1.3 数据迁移（Week 2-3）

- [ ] **迁移脚本开发**
  
  ```python
  # scripts/migrate_sqlite_to_postgres.py
  async def migrate_memories():
      sqlite_store = MemoryStore2("~/.nexus/workspace/memory/store.db")
      pg_store = PostgresMemoryStore(config.storage.postgres_url)
  
      # 分批迁移，每批 1000 条
      offset = 0
      batch_size = 1000
  
      while True:
          items = sqlite_store.fetch_batch(offset, batch_size)
          if not items:
              break
  
          await pg_store.bulk_insert(items)
          print(f"已迁移 {offset + len(items)} 条记录")
          offset += batch_size
  
  # 执行
  asyncio.run(migrate_memories())
  ```

- [ ] **数据校验**
  
  ```python
  # scripts/verify_migration.py
  async def verify():
      sqlite_count = sqlite_store.count_all()
      pg_count = await pg_store.count_all()
  
      assert sqlite_count == pg_count, f"记录数不一致: {sqlite_count} vs {pg_count}"
  
      # 抽样检查内容一致性
      sample_ids = random.sample(all_ids, 1000)
      for item_id in sample_ids:
          old = sqlite_store.get_by_id(item_id)
          new = await pg_store.get_by_id(item_id)
          assert old["summary"] == new["summary"]
  ```

- [ ] **双写验证期（Week 3-4）**
  
  ```python
  # 在 MemoryStore2 中加入双写逻辑
  def upsert_item(self, memory_type, summary, embedding, **kwargs):
      # 写入 SQLite（主）
      result_old = self._sqlite_upsert(memory_type, summary, embedding, **kwargs)
  
      # 异步写入 PostgreSQL（影子）
      asyncio.create_task(
          self._pg_upsert(memory_type, summary, embedding, **kwargs)
      )
  
      return result_old  # 暂时以 SQLite 结果为准
  ```

- [ ] **双写一致性监控**
  
  - [ ] 设置告警：不一致率 > 0.1% 触发
  - [ ] 每小时对比 100 条随机记录
  - [ ] 持续 1 周无问题后切换

### 1.4 切换到 PostgreSQL（Week 4 末）

- [ ] **配置切换**
  
  ```toml
  # config.toml
  [storage]
  backend = "postgres"  # 从 "sqlite" 改为 "postgres"
  ```

- [ ] **灰度发布**
  
  - [ ] 10% 流量切换到新架构（监控 24 小时）
  - [ ] 50% 流量（监控 24 小时）
  - [ ] 100% 流量

- [ ] **SQLite 保留期**
  
  - [ ] 保留 SQLite 文件 2 周
  - [ ] 每日备份到对象存储
  - [ ] Week 6 后归档删除

---

## 阶段 2：异步架构改造（Week 5-8）

### 2.1 LLM 客户端改造（Week 5）

- [ ] **实现异步客户端**
  
  ```python
  # agent/model_runtime/async_client.py
  class AsyncLLMClient:
      def __init__(self, base_url: str, api_keys: list[str]):
          self.http_client = httpx.AsyncClient(
              timeout=httpx.Timeout(120.0),
              limits=httpx.Limits(
                  max_connections=100,
                  max_keepalive_connections=20
              )
          )
          self.rate_limiter = SlidingWindowRateLimiter(
              max_requests=50, 
              window_seconds=60
          )
          self.api_keys = cycle(api_keys)
  ```

- [ ] **速率限制器**
  
  ```python
  # agent/model_runtime/rate_limiter.py
  class SlidingWindowRateLimiter:
      async def acquire(self):
          async with self.lock:
              now = time.time()
              # 清理过期请求
              while self.requests and self.requests[0] < now - self.window:
                  self.requests.popleft()
  
              if len(self.requests) >= self.max_requests:
                  wait_time = self.requests[0] + self.window - now + 0.1
                  await asyncio.sleep(wait_time)
  
              self.requests.append(now)
  ```

- [ ] **多 API Key 轮询**
  
  ```toml
  # config.toml
  [llm.main]
  api_keys = [
      "sk-key1",
      "sk-key2",
      "sk-key3"
  ]
  ```

### 2.2 AgentLoop 异步改造（Week 6-7）

- [ ] **消息队列集成**
  
  ```python
  # infra/queue/redis_queue.py
  class RedisMessageQueue:
      async def push(self, message: dict):
          await self.redis.rpush("nexus:inbox", json.dumps(message))
  
      async def pop_batch(self, max_size: int = 10, timeout: int = 5):
          messages = []
          for _ in range(max_size):
              msg = await self.redis.blpop("nexus:inbox", timeout=timeout)
              if msg:
                  messages.append(json.loads(msg[1]))
              else:
                  break
          return messages
  ```

- [ ] **异步 Worker 实现**
  
  ```python
  # workers/async_agent_worker.py
  class AsyncAgentWorker:
      async def run(self):
          while True:
              batch = await self.queue.pop_batch(max_size=10)
  
              # 并发处理批次
              tasks = [self._handle_message(msg) for msg in batch]
              await asyncio.gather(*tasks, return_exceptions=True)
  
      async def _handle_message(self, msg):
          try:
              session = await self.session_manager.get(msg["session_key"])
              response = await self.agent.run_turn(session, msg["content"])
              await self.send_response(msg["channel"], response)
          except Exception as e:
              logger.error(f"处理消息失败: {e}")
              await self.dlq.push(msg)  # 进入死信队列
  ```

- [ ] **渐进式替换**
  
  ```python
  # main.py 支持两种模式
  def start_agent_loop(mode: str = "sync"):
      if mode == "async":
          from workers.async_agent_worker import AsyncAgentWorker
          workers = [AsyncAgentWorker(i) for i in range(10)]
          asyncio.run(asyncio.gather(*[w.run() for w in workers]))
      else:
          from agent.looping.core import AgentLoop
          AgentLoop().run()
  ```

### 2.3 性能测试（Week 8）

- [ ] **基准测试**
  
  ```bash
  # 使用 Locust 压力测试
  locust -f tests/load/test_message_processing.py \
         --users 1000 \
         --spawn-rate 50 \
         --host http://localhost:8000
  ```

- [ ] **性能指标验证**
  
  - [ ] QPS 达到 50+：________
  - [ ] P95 延迟 < 3秒：________
  - [ ] 错误率 < 0.1%：________
  - [ ] Worker CPU 利用率 60-80%：________

---

## 阶段 3：向量检索调优（并入 Week 3-4，不再是独立阶段）

> **方案已定：pgvector + 分区 HNSW**，不部署独立向量数据库。
> 分区表与每分区 HNSW 已在阶段 1 落地（`alembic/versions/a3d5c7e9f1b2_*`），
> 本阶段只剩参数调优与运维。依据见
> `docs/tasks/phase1-storage/vector-validation.md`。

### 3.1 已完成（阶段 1 M2）

- [x] `memory_items` 按 `tenant_id` LIST 分区
- [x] 每分区独立 HNSW 索引（`m=16, ef_construction=64`）
- [x] 分区懒创建 + advisory lock 防并发 race
- [x] 不建 DEFAULT 分区（会混装多租户，破坏隔离前提）
- [x] 召回验证：小租户 0.180（全局过滤）→ 0.990（分区隔离）

### 3.2 参数调优（真实 1024 维数据上复测）

> 阶段 1 的验证用 128 维合成数据，维度与分布都与生产不同，需复测。

- [ ] **`ef_search` 分档**

  ```sql
  -- 大分区（>50k 行）：ef=40 通常足够
  SET hnsw.ef_search = 40;
  -- 小分区：分区内行数少，planner 可能选 seq scan（召回 1.0，延迟可接受）
  -- 需实测拐点，确认 planner 判断正确
  EXPLAIN (ANALYZE) SELECT id FROM memory_items
  WHERE tenant_id = 'chat_alice'
  ORDER BY embedding <=> %s::vector LIMIT 10;
  ```

- [ ] **`m` / `ef_construction` 复测**：1024 维下召回与构建耗时的权衡
- [ ] **召回基线入库**：按 `tenant_bucket`（分区规模分档）记录召回率，
      作为 `vector_search_recall` 指标的告警基线（< 0.9 告警）

### 3.3 分区规模的运维边界

- [ ] **分区数量上限实测**：PG 在数千分区后 planner 规划耗时上升，
      5000 tenant 需测 `EXPLAIN` 自身开销

  ```sql
  -- 观察 planning time 随分区数增长
  EXPLAIN (ANALYZE, TIMING) SELECT ... ;
  -- 关注 "Planning Time" 一行
  ```

  - [ ] 若超出舒适区：改为按 tenant 哈希分组（多 tenant 共享分区），
        **但需重新验证召回** —— 分组会把小租户混装，正是坍缩成因

- [ ] **索引维护**：分区各自 `REINDEX`，避免单次全表重建阻塞

  ```sql
  REINDEX INDEX CONCURRENTLY memory_items_<tid>_embedding_idx;
  ```

- [ ] **空分区回收**：tenant 注销后 `DROP TABLE` 分区（比 `DELETE` 干净，
      直接释放索引空间）
- [ ] **PG 内存核对**：活跃分区的 HNSW 索引需驻留内存，确认
      `shared_buffers` + 系统缓存覆盖热分区总索引大小

  ```sql
  SELECT pg_size_pretty(sum(pg_relation_size(indexrelid)))
  FROM pg_stat_user_indexes WHERE indexrelname LIKE '%embedding%';
  ```

### 3.4 换引擎的判断依据（保留，避免过早迁移）

以下条件均未触发前，pgvector 是正确选择：

- [ ] 单 tenant 记忆量 > 500 万条（分区内 HNSW 本身成为瓶颈）
- [ ] 需要跨 tenant 全局语义检索（分区隔离恰好挡住此能力）
- [ ] 分区数超 planner 舒适区，且哈希分组后召回不达标

---

## 阶段 4：生产环境部署（Week 9-10）

### 4.1 基础设施（Week 9）

- [ ] **Kubernetes 集群**（或 Docker Swarm）
  
  ```yaml
  # k8s/deployment.yaml
  apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: nexus-worker
  spec:
    replicas: 10
    selector:
      matchLabels:
        app: nexus-worker
    template:
      spec:
        containers:
        - name: worker
          image: nexus:latest
          env:
          - name: WORKER_MODE
            value: "async"
          - name: POSTGRES_URL
            valueFrom:
              secretKeyRef:
                name: db-secret
                key: url
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
  ```

- [ ] **负载均衡**
  
  ```nginx
  # nginx.conf
  upstream nexus_api {
      least_conn;  # 最少连接算法
      server worker1:8000 max_fails=3 fail_timeout=30s;
      server worker2:8000 max_fails=3 fail_timeout=30s;
      server worker3:8000 max_fails=3 fail_timeout=30s;
  }
  
  server {
      listen 443 ssl http2;
      server_name api.nexus.example.com;
  
      location / {
          proxy_pass http://nexus_api;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
      }
  }
  ```

### 4.2 监控告警（Week 9）

- [ ] **Prometheus 指标**
  
  ```python
  # infra/monitoring/metrics.py
  from prometheus_client import Counter, Histogram, Gauge
  
  message_processed = Counter("nexus_messages_processed_total", "Total messages")
  message_latency = Histogram("nexus_message_latency_seconds", "Message processing time")
  active_sessions = Gauge("nexus_active_sessions", "Active user sessions")
  queue_depth = Gauge("nexus_queue_depth", "Messages in queue")
  ```

- [ ] **Grafana 仪表盘**
  
  - [ ] 消息处理 QPS
  - [ ] 平均响应时间（P50/P95/P99）
  - [ ] 数据库连接池使用率
  - [ ] Redis 命中率
  - [ ] Worker CPU/内存使用

- [ ] **告警规则**
  
  ```yaml
  # prometheus/alerts.yml
  groups:
  - name: nexus
    rules:
    - alert: HighErrorRate
      expr: rate(nexus_errors_total[5m]) > 0.01
      for: 5m
      annotations:
        summary: "错误率过高 ({{ $value }})"
  
    - alert: HighLatency
      expr: histogram_quantile(0.95, nexus_message_latency_seconds) > 5
      for: 10m
      annotations:
        summary: "P95 延迟 > 5秒"
  ```

### 4.3 灰度发布（Week 10）

- [ ] **流量切换计划**
  
  | 阶段   | 流量比例 | 持续时间 | 回滚条件          |
  | ---- | ---- | ---- | ------------- |
  | 金丝雀  | 1%   | 24小时 | 错误率 > 1%      |
  | 小范围  | 10%  | 48小时 | P95 > 5秒      |
  | 中等规模 | 50%  | 72小时 | 数据库 CPU > 90% |
  | 全量上线 | 100% | -    | -             |

- [ ] **Feature Flag 控制**
  
  ```toml
  # config.toml
  [features]
  use_postgres = true          # 存储后端
  use_async_loop = true         # 异步处理
  use_vector_cache = true       # 向量结果缓存
  rate_limit_enabled = true     # LLM 限流
  ```

---

## 阶段 4.5：WebChat 补完（Week 11-13，仅当启用 webchat）

### 4.5.1 出站 Pub/Sub 与 Gateway 拆分（Week 11）

> 前置：Week 6 的出站接口须已定为 async generator（`OutboundChunk`），否则此处要
> 重构整条调用链。

- [ ] **实现 OutboundBus**

  ```python
  # infra/queue/outbound_bus.py
  async def publish(self, chunk: OutboundChunk) -> None:
      await self.redis.publish(
          f"nexus:out:{chunk.session_key}", json.dumps(asdict(chunk))
      )
  ```

- [ ] **Worker 侧改为 publish**，不再依赖进程内 writer 映射
- [ ] **Gateway 侧订阅**：WS 连接建立时 subscribe，关闭时 unsubscribe
- [ ] **落库与推送分离**：完整回复由 Worker 直写 PostgreSQL；Pub/Sub 仅实时推送
      （at-most-once，不可当持久化）
- [ ] **送达率验证**：10 Worker 下入站消息数 = 出站送达数，差值非零即告警

- [ ] **Nginx WS 透传**

  ```nginx
  location /ws {
      proxy_pass http://nexus_gateway;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 3600s;
  }
  ```

- [ ] **确认无需 sticky session**：断开重连落到另一实例仍能收到消息

- [ ] **fd 上限调整**

  ```bash
  # /etc/security/limits.conf
  nexus soft nofile 65535
  nexus hard nofile 65535
  ```

### 4.5.2 鉴权与 channel 实现（Week 12）

> Phase 5.1 鉴权不依赖存储层，可提前到 Week 3-4 与数据迁移并行。

- [ ] **创建 `infra/channels/web_chat_channel.py`**（当前不存在，设
      `enabled = true` 会 ImportError）

  - [ ] 满足 `Channel` 协议：`name` / `start(ctx)` / `stop()`
  - [ ] `handle_websocket` / `save_upload` / `upload_roots` / `has_media` /
        `_require_ctx`（`chat_api.py` 依赖）

- [ ] **session_key 与 tenant_id 均服务端派生**

  ```python
  def tenant_id_for(self, user_id: str) -> str:
      return f"chat_{user_id}"       # 分区键，物理隔离的依据
  def session_key_for(self, user_id: str) -> str:
      return f"chat:{user_id}"       # 来自 token.sub，前端传值一律忽略
  ```

- [ ] **WS 握手鉴权**：`?token=` 校验失败即 `close(4401)`，不进消息循环
- [ ] **REST 端点加 `Depends(require_user)`**：`/api/chat/sessions`、
      `/messages`、`/media`、`/uploads`
- [ ] **`tenant_id` 只能来自 token，禁止从请求参数读取**

  - [ ] store 实例按请求身份取（`PostgresMemoryStore(url, tenant_id=...)` 已把
        tenant 绑在实例上，不需给每个查询方法加参数）
  - [ ] 审查 `bootstrap/chat_api.py` 所有端点，确认无 `tenant_id` 入参
  - [ ] 一旦可从请求传入，分区隔离即失效

- [ ] **附件归属校验**：`_can_read_media` 之外追加"该文件属于请求者会话"判断
- [ ] **启动自检**：`host` 非环回且 `auth.enabled = false` 时 `raise ConfigError`
      （不是 warning）

  ```toml
  [channels.chat.auth]
  enabled = true
  mode = "jwt"
  secret = "${CHAT_JWT_SECRET}"
  token_ttl_seconds = 86400
  ```

- [ ] **入队前限流**

  ```toml
  [channels.chat.limits]
  max_messages_per_minute = 20
  max_upload_bytes = 10485760
  max_sessions_per_user = 50
  ```

  - [ ] 确认限流发生在 publish_inbound 之前（Worker 内限流等于额度已被消耗）

- [ ] **前端构建**：复用 `frontend/dashboard/` 的 Vite 链路

  ```jsonc
  "scripts": { "build:chat": "vite build --outDir ../../static/chat" }
  ```

  - [ ] `.gitignore` 加 `/static/chat/`

- [ ] **连接配置独立**（不复用 `AppServerConfig.max_connections = 32`）

  ```toml
  [channels.chat]
  max_connections = 6000
  heartbeat_interval_seconds = 30
  idle_timeout_seconds = 300
  ```

### 4.5.3 专项测试与上线（Week 13）

- [ ] **越权测试进 CI**

  - [ ] 持 A 的 token 请求 B 的会话列表 → 403
  - [ ] 持 A 的 token 请求 B 的 `session_key` 消息 → 403
  - [ ] 持 A 的 token 读 B 的附件 → 403/404
  - [ ] 前端伪造 `session_key` → 服务端仍按 token 派生
  - [ ] 无 token 访问全部端点 → 401

- [ ] **配置安全测试**：`host = "0.0.0.0"` + `auth.enabled = false` → 启动失败
- [ ] **负载测试**

  - [ ] 5000 并发 WS 不触发 `EMFILE`
  - [ ] 连接态内存占用在 100-250MB 区间
  - [ ] 空闲 10 分钟连接不被反代切断

- [ ] **流式渲染**：前端逐字显示，末帧 `tool_chain` 正确落地
- [ ] **重连补齐**：断开期间的回复可从
      `/api/chat/sessions/{key}/messages` 拉回

- [ ] **单 Worker 降级预案**：若 4.5.1 未完成而需提前上线，配置中显式锁
      `worker_count = 1` 并记录该约束

---

## 阶段 5：监控与优化（持续）

> **注意**：基础埋点应在 Week 1-2 就绪（与阶段 1 并行），因为 Week 3-4 的双写
> 一致性验证依赖 `dual_write_mismatch` 指标 —— 没有埋点只能人工抽查。本阶段是
> 上线后的持续调优，不是埋点的起点。

### 5.1 性能调优清单

- [ ] **数据库优化**
  
  ```sql
  -- 定期 VACUUM
  VACUUM ANALYZE memory_items;
  
  -- 检查慢查询
  SELECT query, mean_exec_time, calls
  FROM pg_stat_statements
  ORDER BY mean_exec_time DESC
  LIMIT 20;
  ```

- [ ] **Redis 优化**
  
  ```bash
  # 监控内存碎片
  redis-cli info memory | grep fragmentation
  
  # 碎片率 > 1.5 时整理
  redis-cli memory purge
  ```

- [ ] **代码性能分析**
  
  ```python
  # 使用 py-spy 分析热点
  py-spy record -o profile.svg -- python main.py
  ```

### 5.2 容量规划

- [ ] **每月检查**
  
  - [ ] 数据库磁盘使用率 < 70%
  - [ ] Redis 内存使用率 < 80%
  - [ ] Worker CPU 平均负载 < 75%
  - [ ] 消息队列积压 < 1000 条

- [ ] **扩容触发条件**
  
  - [ ] P95 延迟持续 1 周 > 3 秒 → 加 Worker
  - [ ] 数据库 CPU > 80% 持续 3 天 → 升级规格
  - [ ] 向量检索 QPS > 80 → 加 PG 只读副本 / 调 ef_search

---

## 回滚应急预案

### 紧急回退到 SQLite

```bash
# 1. 停止所有 Worker
kubectl scale deployment nexus-worker --replicas=0

# 2. 修改配置
sed -i 's/backend = "postgres"/backend = "sqlite"/' config.toml

# 3. 从最新备份恢复 SQLite
cp backups/store_$(date +%Y%m%d).db ~/.nexus/workspace/memory/store.db

# 4. 重启服务
python main.py &
```

**预估回滚时间**：5-10 分钟

---

## 验收标准

### 功能验收

- [ ] 所有现有功能正常（Telegram/QQ/CLI）
- [ ] 记忆检索结果一致性 > 95%
- [ ] 无数据丢失（对比迁移前后记录数）

### 性能验收

- [ ] 消息处理 QPS ≥ 50
- [ ] P95 响应延迟 ≤ 3 秒
- [ ] 向量检索延迟 ≤ 100ms
- [ ] 数据库连接池利用率 < 80%

### 可靠性验收

- [ ] 单 Worker 宕机不影响服务
- [ ] 数据库主从切换时间 < 30 秒
- [ ] 连续 7 天无严重告警（P0/P1）

### 成本验收

- [ ] 单用户月均成本 ≤ $0.50
- [ ] LLM API 费用增长 < 20%

### 安全验收（webchat 启用时）

- [ ] **webchat 端点在 `0.0.0.0` 下全部需要鉴权** —— 从单机自托管转向多用户服务时
      最容易漏、后果最直接的一项：后果是全量会话与记忆泄露，无告警、不可回滚
- [ ] `session_key` 由服务端从 token 派生，前端不可指定
- [ ] 越权测试已进 CI（防后续改动静默回退）
- [ ] `host` 非环回且鉴权关闭时启动失败，而非 warning

---

## 附录：常用命令

### 数据库操作

```bash
# 连接 PostgreSQL
psql -U nexus_app -d nexus_production

# 查看表大小
SELECT 
    schemaname, 
    tablename, 
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables 
WHERE schemaname = 'public';

# 查看索引大小
SELECT 
    indexname, 
    pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes;
```

### Redis 操作

```bash
# 查看队列长度
redis-cli llen nexus:inbox

# 查看缓存命中率
redis-cli info stats | grep keyspace

# 清空测试数据
redis-cli flushdb
```

### Kubernetes 操作

```bash
# 查看 Worker 状态
kubectl get pods -l app=nexus-worker

# 查看日志
kubectl logs -f nexus-worker-xxx

# 扩容 Worker
kubectl scale deployment nexus-worker --replicas=15

# 滚动更新
kubectl set image deployment/nexus-worker worker=nexus:v2.0.0
```
