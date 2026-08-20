# 迁移执行清单（5000用户扩展）

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
  qdrant-client>=1.7.0  # 可选
  prometheus-client>=0.19.0
  ```

- [ ] **监控工具**
  
  - [ ] Grafana + Prometheus 搭建
  - [ ] 日志聚合（ELK 或 Loki）

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

## 阶段 3：向量检索优化（Week 9-10）

### 3.1 Qdrant 部署（可选）

- [ ] **Docker 部署**
  
  ```yaml
  # docker-compose.yml
  services:
    qdrant:
      image: qdrant/qdrant:v1.7.4
      ports:
        - "6333:6333"
      volumes:
        - ./qdrant_storage:/qdrant/storage
      environment:
        - QDRANT__SERVICE__HTTP_PORT=6333
  ```

- [ ] **数据导入**
  
  ```python
  # scripts/import_to_qdrant.py
  async def import_vectors():
      qdrant = AsyncQdrantClient(url="http://localhost:6333")
  
      # 创建 collection
      await qdrant.create_collection(
          collection_name="memories",
          vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
      )
  
      # 批量导入
      pg_store = PostgresMemoryStore(...)
      items = await pg_store.get_all_with_embedding()
  
      points = [
          PointStruct(
              id=item["id"],
              vector=item["embedding"],
              payload={
                  "session_key": item["session_key"],
                  "summary": item["summary"],
                  "memory_type": item["memory_type"]
              }
          )
          for item in items
      ]
  
      await qdrant.upsert(collection_name="memories", points=points)
  ```

### 3.2 pgvector 优化（备选方案）

- [ ] **HNSW 索引调优**
  
  ```sql
  -- 重建索引（离线操作，需停机维护窗口）
  DROP INDEX IF EXISTS idx_memory_embedding;
  
  CREATE INDEX idx_memory_embedding ON memory_items 
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
  
  -- 查询时调整 ef_search 参数
  SET hnsw.ef_search = 100;  -- 默认 40，提高召回率
  ```

- [ ] **分区表**
  
  ```sql
  -- 按 session_key 分区
  CREATE TABLE memory_items_partitioned (
      LIKE memory_items INCLUDING ALL
  ) PARTITION BY HASH (session_key);
  
  -- 创建 10 个分区
  CREATE TABLE memory_items_p0 PARTITION OF memory_items_partitioned
      FOR VALUES WITH (MODULUS 10, REMAINDER 0);
  -- ... 重复到 p9
  ```

### 3.3 性能对比测试

- [ ] **检索延迟测试**
  
  ```python
  # tests/benchmark_vector_search.py
  async def benchmark():
      query_vec = [random.random() for _ in range(1024)]
  
      # 测试 pgvector
      start = time.time()
      results_pg = await pg_store.vector_search(query_vec, top_k=10)
      pg_latency = time.time() - start
  
      # 测试 Qdrant
      start = time.time()
      results_qd = await qdrant.search(query_vec, limit=10)
      qd_latency = time.time() - start
  
      print(f"pgvector: {pg_latency*1000:.2f}ms")
      print(f"Qdrant: {qd_latency*1000:.2f}ms")
  ```

---

## 阶段 4：生产环境部署（Week 11-12）

### 4.1 基础设施（Week 11）

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

### 4.2 监控告警（Week 11）

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

### 4.3 灰度发布（Week 12）

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

## 阶段 4.5：WebChat 补完（Week 13-15，仅当启用 webchat）

### 4.5.1 出站 Pub/Sub 与 Gateway 拆分（Week 13）

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

### 4.5.2 鉴权与 channel 实现（Week 14）

> Phase 5.1 鉴权不依赖存储层，可提前到 Week 3-4 与数据迁移并行。

- [ ] **创建 `infra/channels/web_chat_channel.py`**（当前不存在，设
      `enabled = true` 会 ImportError）

  - [ ] 满足 `Channel` 协议：`name` / `start(ctx)` / `stop()`
  - [ ] `handle_websocket` / `save_upload` / `upload_roots` / `has_media` /
        `_require_ctx`（`chat_api.py` 依赖）

- [ ] **session_key 服务端派生**

  ```python
  def session_key_for(self, user_id: str) -> str:
      return f"chat:{user_id}"      # 来自 token.sub，前端传值一律忽略
  ```

- [ ] **WS 握手鉴权**：`?token=` 校验失败即 `close(4401)`，不进消息循环
- [ ] **REST 端点加 `Depends(require_user)`**：`/api/chat/sessions`、
      `/messages`、`/media`、`/uploads`
- [ ] **`owner_id` 改为必填参数**（不是 `str | None = None`）

  - [ ] `list_sessions_for_dashboard`（`session/store.py:642`）
  - [ ] `list_messages_for_dashboard`
  - [ ] 可选参数会被漏传，且漏传时静默返回全量数据

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

### 4.5.3 专项测试与上线（Week 15）

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
  - [ ] 向量检索 QPS > 80 → 加 Qdrant 节点

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
