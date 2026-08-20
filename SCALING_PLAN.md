# NexusCompanion 5000 用户扩展方案

## 当前架构瓶颈

### 1. 存储层（最紧迫）

- **SQLite 写入瓶颈**：`memory2/store.py` 和 `session/store.py` 都使用 SQLite
  
  - 单个文件数据库，写入串行化
  - 5000 用户并发写入会排队等待锁
  - 文件 I/O 无法分布式扩展

- **向量检索性能**：`sqlite-vec` 适合单机，但：
  
  - 向量数据全在内存，5000 用户记忆会占用 10-50GB+
  - 无法横向扩展，单机内存有上限
  - 检索 QPS 受限于单核 CPU

### 2. 并发架构

- **单进程 + threading.Lock**：所有存储操作用 `threading.RLock()`
  
  - Python GIL 限制，无法利用多核
  - 无法水平扩展到多台机器

- **同步阻塞设计**：LLM 调用、文件 I/O 都是同步的
  
  - 一个慢请求会阻塞整个进程
  - 5000 用户场景下响应时间会显著劣化

### 3. LLM API 管理

- **无连接池**：每次调用新建 HTTP 连接
- **无速率限制**：高并发会触发 API 限流（如 DeepSeek 60 RPM）
- **无请求队列**：突发流量会导致大量失败

### 4. 内存管理

- **记忆系统内存占用**：
  - 每个用户平均 1000 条记忆 × 1024 维向量 = ~4MB
  - 5000 用户 × 4MB = 20GB 基础内存
  - 加上 embedding cache、session cache，需要 30-50GB

### 5. WebChat 半成品状态

`bootstrap/chat_api.py` 与 `bootstrap/app.py:366-372, 435-445` 的接线已存在，
`ChatChannelConfig`（`agent/config_models.py:44`）也已定义，但
`infra/channels/web_chat_channel.py` **不存在**。当前设 `[channels.chat] enabled = true`
会在启动时 ImportError。

补完这个 channel 时有三个与其它 channel 不同的结构性问题，直接影响扩展方案：

- **身份自持**：Telegram/QQ 的 chat_id 由平台保证不可伪造，`allow_from` 白名单足够；
  webchat 的身份必须自己签发，否则 session_key 可被前端任意指定。
- **连接常驻**：webhook 无状态，WebSocket 是每标签页一条常驻 TCP。5000 在线意味着
  5000 fd，且连接与进程绑定 —— 与 Phase 4 的多 Worker 直接冲突。
- **流式预期**：webchat 用户预期逐字输出，而当前 `OutboundMessage` 是一次性 content。
  这决定了 Phase 2 的出站接口该做成什么形状。

`chat_api.py` 现有端点无任何鉴权：`/ws` 直接进 `handle_websocket`，
`/api/chat/sessions` 列全部会话，`/api/chat/media?path=` 按绝对路径读文件。默认
`host = "127.0.0.1"` 挡住了这些，但多用户部署必然改 `0.0.0.0` + 反代，那一刻这些
端点即为敞开状态。`_can_read_media` 已做 `upload_roots` 路径校验，方向正确，但只防
路径穿越，未校验请求者是否有权访问该文件。

---

## 扩展改造方案（按优先级）

## Phase 1: 存储层改造（P0 - 必须）

### 1.1 迁移到 PostgreSQL + pgvector

**为什么**：SQLite 无法支撑 5000 用户写并发

**改动点**：

```python
# 新增文件：infra/storage/postgres_store.py
import asyncpg
from pgvector.asyncpg import register_vector

class PostgresMemoryStore:
    def __init__(self, dsn: str, pool_size: int = 20):
        self.pool = await asyncpg.create_pool(
            dsn, 
            min_size=10, 
            max_size=pool_size,
            command_timeout=60
        )
        await register_vector(self.pool)

    async def vector_search(self, query_vec, top_k=8):
        # 使用 pgvector 的 <-> 运算符
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT id, summary, 1 - (embedding <-> $1) as score
                FROM memory_items
                WHERE status = 'active'
                ORDER BY embedding <-> $1
                LIMIT $2
            """, query_vec, top_k)
```

**配置变更**：

```toml
# config.toml 新增
[storage]
backend = "postgres"  # 或 "sqlite" 兼容单机部署
postgres_url = "postgresql://user:pass@localhost/nexus"
pool_min_size = 10
pool_max_size = 50  # 5000用户 / 100并发 ≈ 50连接
```

**迁移脚本**：

```python
# scripts/migrate_sqlite_to_postgres.py
async def migrate():
    sqlite_store = MemoryStore2("~/.nexus/workspace/memory/store.db")
    pg_store = PostgresMemoryStore(config.storage.postgres_url)

    # 批量迁移，每批 1000 条
    items = sqlite_store.get_all_with_embedding()
    await pg_store.bulk_insert(items, batch_size=1000)
```

### 1.2 引入 Redis 缓存层

**为什么**：减少数据库读压力，提升响应速度

**改动点**：

```python
# infra/cache/redis_cache.py
import redis.asyncio as redis

class MemoryCache:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)

    async def get_recent_memory(self, session_key: str):
        cached = await self.redis.get(f"mem:{session_key}")
        if cached:
            return json.loads(cached)
        # Cache miss，从数据库加载
        items = await db.fetch_recent(session_key, limit=20)
        await self.redis.setex(
            f"mem:{session_key}", 
            300,  # 5分钟过期
            json.dumps(items)
        )
        return items
```

**缓存策略**：

- **会话上下文**：5分钟 TTL，减少 90% session 读
- **用户画像**：1小时 TTL，MEMORY.md 热数据
- **向量检索结果**：query hash 做 key，10分钟缓存

---

## Phase 2: 异步架构改造（P0 - 必须）

### 2.1 LLM 调用改为异步 + 连接池

**改动点**：

```python
# agent/model_runtime/transports/async_client.py
import httpx

class AsyncLLMClient:
    def __init__(self, base_url: str, api_key: str):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(120.0),
            limits=httpx.Limits(
                max_connections=100,      # 连接池大小
                max_keepalive_connections=20
            )
        )

    async def chat_completion(self, messages, **kwargs):
        # 加入重试机制
        for attempt in range(3):
            try:
                resp = await self.client.post(
                    "/chat/completions",
                    json={"messages": messages, **kwargs},
                    headers={"Authorization": f"Bearer {self.api_key}"}
                )
                return resp.json()
            except httpx.TimeoutException:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
```

### 2.2 加入速率限制器

**改动点**：

```python
# agent/model_runtime/rate_limiter.py
import asyncio
from collections import deque

class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            now = time.time()
            # 清理过期请求
            while self.requests and self.requests[0] < now - self.window:
                self.requests.popleft()

            if len(self.requests) >= self.max_requests:
                # 等待最老的请求过期
                wait_time = self.requests[0] + self.window - now
                await asyncio.sleep(wait_time + 0.1)

            self.requests.append(now)

# 使用
rate_limiter = SlidingWindowRateLimiter(
    max_requests=50,  # DeepSeek 60 RPM，预留 buffer
    window_seconds=60
)

async def call_llm(messages):
    await rate_limiter.acquire()
    return await client.chat_completion(messages)
```

### 2.3 AgentLoop 改为异步

**改动点**：

```python
# agent/looping/core.py (重构)
class AsyncAgentLoop:
    async def run(self):
        # 并发处理多个用户消息
        async with asyncio.TaskGroup() as tg:
            while True:
                batch = await self.message_bus.get_batch(max_size=10)
                for msg in batch:
                    tg.create_task(self._handle_message(msg))

    async def _handle_message(self, msg):
        # 每个消息独立协程处理
        session = await self.session_manager.get(msg.session_key)
        # ... 原有 turn 逻辑
```

### 2.4 出站接口做成流式（为 webchat 预留）

**为什么放在 Phase 2**：这是顺手与返工的分界。异步化时如果把出站定成
`async def -> str`，webchat 上线时要再改一遍同样的调用链；定成 async generator 则
流式是免费的，Telegram 侧只需在末端聚合，行为不变。

```python
# bus/events.py —— 出站从一次性 content 改为增量流
@dataclass
class OutboundChunk:
    session_key: str
    delta: str            # 增量文本
    done: bool = False    # 末帧标记
    tool_chain: list | None = None   # 仅末帧携带

# agent/looping/core.py
class AsyncAgentLoop:
    async def stream_turn(self, msg) -> AsyncIterator[OutboundChunk]:
        async for delta in self.reasoner.stream(msg):
            yield OutboundChunk(msg.session_key, delta)
        yield OutboundChunk(msg.session_key, "", done=True)
```

**各 channel 的消费方式**：

| Channel | 消费方式 |
|---------|----------|
| webchat | 每个 chunk 直接 `websocket.send_json`，逐字渲染 |
| Telegram | 缓冲聚合，末帧一次性 `send_message`（或按节流 `edit_message`）|
| QQ / 飞书 | 同 Telegram，缓冲后整段发 |
| CLI / IPC | 逐帧写 socket，行为即现状 |

聚合成整段是一行 `"".join()`，把整段拆成流则要重构整条链 —— 所以默认走流式。

---

## Phase 3: 向量检索优化（P1 - 重要）

### 3.1 切换到专用向量数据库

**选项 A：Qdrant（推荐）**

```python
# infra/vector/qdrant_client.py
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

class QdrantMemoryStore:
    def __init__(self, url: str):
        self.client = AsyncQdrantClient(url=url)

    async def init_collection(self):
        await self.client.create_collection(
            collection_name="memories",
            vectors_config=VectorParams(
                size=1024, 
                distance=Distance.COSINE
            )
        )

    async def vector_search(self, query_vec, top_k=8, filters=None):
        results = await self.client.search(
            collection_name="memories",
            query_vector=query_vec,
            limit=top_k,
            query_filter=filters  # 按 session_key 过滤
        )
        return [hit.payload for hit in results]
```

**选项 B：继续用 pgvector（成本低）**

- 创建 HNSW 索引加速检索：
  
  ```sql
  CREATE INDEX ON memory_items 
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
  ```

### 3.2 分片策略

**按用户分片**：

```python
# 每个用户的记忆独立存储，避免全表扫描
collection_name = f"memories_shard_{hash(session_key) % 10}"
```

---

## Phase 4: 水平扩展架构（P1 - 重要）

### 4.1 引入消息队列（解耦）

**架构图**：

```
Telegram/QQ Bot → Redis Queue → Worker Pool (多进程) → Database
                                    ↓
                               LLM API (with rate limiter)
```

**改动点**：

```python
# workers/message_worker.py
import asyncio
from redis import asyncio as aioredis

class MessageWorker:
    def __init__(self, worker_id: int, redis_url: str):
        self.worker_id = worker_id
        self.redis = aioredis.from_url(redis_url)

    async def run(self):
        while True:
            # 从队列拉取消息
            msg = await self.redis.blpop("nexus:inbox", timeout=5)
            if msg:
                await self.process_message(json.loads(msg[1]))

    async def process_message(self, msg_data):
        # 原有的 PassiveTurn 逻辑
        session = await get_session(msg_data["session_key"])
        agent = build_agent(session)
        response = await agent.run_turn(msg_data["content"])
        # 回传到频道
        await send_response(msg_data["channel"], response)

# 启动多个 worker
async def main():
    workers = [MessageWorker(i, REDIS_URL) for i in range(10)]
    await asyncio.gather(*[w.run() for w in workers])
```

### 4.1b 出站也必须 Pub/Sub（WebSocket 前置条件）

**问题**：入站队列解决了负载分发，但出站会断。`ipc_server.py:57` 用
`self._writers: dict[chat_id, StreamWriter]` 维护进程内连接映射 —— 单进程下成立，
多 Worker 下失效：

```
用户 WS 连在 Gateway-A  →  消息入队  →  Worker-B 取出处理
                                          ↓
                            Worker-B 的 _writers 里没有这个连接
                                          ↓
                                    回复静默丢失
```

Telegram/QQ 不受影响（Worker 直接调平台 HTTP API 即可送达），**只有 WebSocket 这类
有状态长连接会中招**。所以这一项是 webchat 上多 Worker 的硬前置，不是可选优化。

**解法**：出站走 Redis Pub/Sub，连接归属与消息处理解耦。

```python
# infra/queue/outbound_bus.py
class OutboundBus:
    """Worker 发布 → 持有连接的进程订阅 → 写回 socket"""

    async def publish(self, chunk: OutboundChunk) -> None:
        await self.redis.publish(
            f"nexus:out:{chunk.session_key}",
            json.dumps(asdict(chunk)),
        )

    async def subscribe(self, session_key: str) -> AsyncIterator[OutboundChunk]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"nexus:out:{session_key}")
        try:
            async for raw in pubsub.listen():
                if raw["type"] == "message":
                    yield OutboundChunk(**json.loads(raw["data"]))
        finally:
            await pubsub.unsubscribe(f"nexus:out:{session_key}")
```

Gateway 侧在 WS 连接建立时订阅该 session 频道，关闭时取消订阅：

```python
# infra/channels/web_chat_channel.py
async def handle_websocket(self, ws: WebSocket) -> None:
    session_key = await self._authenticate(ws)   # 见 Phase 5.1
    await ws.accept()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(self._pump_outbound(ws, session_key))
        tg.create_task(self._pump_inbound(ws, session_key))
```

**注意投递语义**：Redis Pub/Sub 是 at-most-once，订阅者掉线期间的消息直接丢弃。对
流式 token 可接受（重连后重新请求），但**末帧与落库不能依赖它** —— 完整回复必须由
Worker 直写 PostgreSQL，Pub/Sub 只负责实时推送。前端重连后从
`/api/chat/sessions/{key}/messages` 拉取补齐。

**规模上限**：单 Redis 实例 Pub/Sub 约支撑万级 channel。超出则改用 Redis Streams +
consumer group（可回溯、有 ACK），或按 session_key 哈希分片到多实例。5000 用户下
单实例足够。

### 4.1c WebSocket 连接数的资源约束

这是 webchat 相对 webhook 最大的成本差异 —— Telegram 5000 用户对进程无常驻开销，
webchat 每个打开的标签页是一条常驻连接。

| 项目 | 说明 |
|------|------|
| fd 数量 | 5000 在线 ≈ 5000 fd，`ulimit -n` 默认 1024，必须调至 65535 |
| 单连接内存 | WS 对象 + 缓冲约 20-50KB，5000 连接 ≈ 100-250MB |
| `max_connections` | `AppServerConfig` 默认 32（为 IPC 设计），webchat 需独立配置项 |
| 心跳 | 需 ping/pong 保活，否则反代（Nginx 默认 60s）切断空闲连接 |
| 反代配置 | Nginx 需 `Upgrade` 头透传 + `proxy_read_timeout 3600s` |

```toml
# config.toml —— webchat 不复用 app_server 的连接上限
[channels.chat]
enabled = true
host = "0.0.0.0"
port = 6322
max_connections = 6000          # 留 20% buffer
heartbeat_interval_seconds = 30
idle_timeout_seconds = 300      # 无心跳响应则回收
```

```bash
# /etc/security/limits.conf
nexus soft nofile 65535
nexus hard nofile 65535
```

**Gateway 与 Worker 分离部署**：Gateway 只做鉴权 + 连接维持 + 队列读写（I/O 密集，
2 核可撑数千连接），Worker 做 LLM 调用（等待密集）。二者资源画像不同，混部会让
Worker 的 GC 停顿影响连接心跳，导致误判掉线。

### 4.2 独立部署模块

**部署拓扑**：

```
┌─────────────────────────────────────────────────┐
│  Nginx (负载均衡 + WS Upgrade 透传)              │
├─────────────────────────────────────────────────┤
│  Gateway 层 (3 实例)                            │
│   - 接收 webhook (Telegram/QQ)                  │
│   - 鉴权 + 维持 WebSocket 常驻连接               │
│   - 入站写 Redis Queue / 出站订阅 Pub/Sub        │
├─────────────────────────────────────────────────┤
│  Worker Pool (10+ 实例)                         │
│   - 消费队列消息                                 │
│   - AgentLoop 处理                              │
│   - LLM 调用                                    │
│   - 流式 chunk 发布到 Pub/Sub                    │
├─────────────────────────────────────────────────┤
│  PostgreSQL (主) + 只读副本 (2台)               │
│  Redis Cluster (缓存 + 队列 + Pub/Sub)          │
│  Qdrant / pgvector (向量检索)                   │
└─────────────────────────────────────────────────┘
```

Gateway 与 Worker 拆开的理由见 4.1c：前者 I/O 密集（几千连接 2 核够用），后者等待
密集（LLM 调用），混部会让 Worker 的 GC 停顿影响连接心跳造成误判掉线。

**反代配置**：WebSocket 需要 Nginx 显式透传 Upgrade 头，否则握手在反代层就被降级成
普通 HTTP：

```nginx
location /ws {
    proxy_pass http://nexus_gateway;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # 默认 60s 会切断空闲长连接
    proxy_send_timeout 3600s;
}
```

因为出站走 Pub/Sub（4.1b），**不需要 sticky session** —— 用户重连落到任意 Gateway
实例都能收到自己的消息。这是把出站解耦后附带拿到的好处，也让 Gateway 可以独立扩缩容。

---

## Phase 5: WebChat 补完（P0 - 与 Phase 4 同步做）

### 5.0 现状与缺口

| 已有 | 缺失 |
|------|------|
| `bootstrap/chat_api.py` 全部 HTTP/WS 端点 | `infra/channels/web_chat_channel.py`（文件不存在）|
| `bootstrap/app.py:366-372` 条件接线 | `static/chat/index.html`（前端产物）|
| `ChatChannelConfig`（`config_models.py:44`）| 任何鉴权 |

`web_chat_channel.py` 需满足 `infra/channels/contract.py` 的 `Channel` 协议
（`name` / `start` / `stop`），另外 `chat_api.py` 还依赖这些成员：

```python
class WebChatChannel:
    name: str
    async def start(self, ctx: ChannelContext) -> None: ...
    async def stop(self) -> None: ...
    async def handle_websocket(self, ws: WebSocket) -> None: ...   # chat_api.py:74
    def save_upload(self, data: bytes, filename: str) -> dict: ... # chat_api.py:85
    def upload_roots(self) -> list[Path]: ...                      # chat_api.py:128
    def has_media(self, path: Path) -> bool: ...                   # chat_api.py:130
    def _require_ctx(self) -> ChannelContext: ...                  # chat_api.py:41
```

### 5.1 认证与授权（P0 中的 P0）

**为什么最高**：其它 Phase 做错的后果是慢或贵，可观测、可回滚；这一项做错的后果是
全量会话与记忆泄露，无告警、不可撤回。

`session_key` 是隔离的信任根，必须服务端派生，前端传值一律忽略：

```python
# infra/channels/web_chat_auth.py
class ChatAuth:
    def issue_token(self, user_id: str) -> str:
        return jwt.encode({"sub": user_id, "exp": ...}, self.secret, "HS256")

    def session_key_for(self, user_id: str) -> str:
        return f"chat:{user_id}"          # 由 token.sub 决定，不接受前端入参
```

WS 握手阶段鉴权（浏览器 WebSocket 无法自定义 header，走 query 参数）：

```python
# infra/channels/web_chat_channel.py
async def _authenticate(self, ws: WebSocket) -> str:
    token = ws.query_params.get("token", "")
    try:
        claims = self._auth.verify(token)
    except InvalidToken:
        await ws.close(code=4401)          # 握手即拒，不进消息循环
        raise
    return self._auth.session_key_for(claims["sub"])
```

REST 端点加依赖，并把归属过滤做成**必填参数**：

```python
# bootstrap/chat_api.py
@app.get("/api/chat/sessions")
def list_sessions(user: User = Depends(require_user), ...):
    items, total = store.list_sessions_for_dashboard(
        channel=channel.name,
        owner_id=user.id,      # 必填，不是 owner_id: str | None = None
        ...
    )
```

`owner_id` 设为可选会被漏传，且漏传时静默返回全量数据 —— 这类缺陷不会在功能测试中
暴露。同理 `/api/chat/media` 需在 `_can_read_media` 的路径校验之外，追加"该文件是否
属于请求者的会话"判断。

```toml
[channels.chat.auth]
enabled = true
mode = "jwt"                 # jwt | shared_token
secret = "${CHAT_JWT_SECRET}"
token_ttl_seconds = 86400
```

**启动自检**：`host` 非环回且 `auth.enabled = false` 时**直接拒绝启动**，不是打
warning。warning 会被忽略。

```python
if config.channels.chat.enabled:
    if config.channels.chat.host not in ("127.0.0.1", "localhost", "::1"):
        if not config.channels.chat.auth.enabled:
            raise ConfigError(
                "webchat 绑定非环回地址时必须启用 [channels.chat.auth]"
            )
```

### 5.2 滥用防护

平台 channel 由 Telegram/QQ 挡住机器人和刷量，webchat 直面公网，需自己做：

```python
# 频率限制必须在入队之前生效
class PerUserRateLimiter:
    async def check(self, user_id: str) -> None:
        n = await self.redis.incr(f"rl:{user_id}:{int(time.time()) // 60}")
        if n == 1:
            await self.redis.expire(f"rl:{user_id}:{...}", 120)
        if n > self.max_per_minute:
            raise RateLimited
```

放在入队前而非 Worker 内是关键：Worker 内限流意味着队列已被填满、LLM 额度已被占用，
限流只是延后了失败。

```toml
[channels.chat.limits]
max_messages_per_minute = 20
max_upload_bytes = 10485760
max_sessions_per_user = 50
```

### 5.3 前端

`frontend/dashboard/` 已有 React + Vite + Tailwind 链路，复用即可，不必新起构建体系：

```jsonc
// frontend/dashboard/package.json
"scripts": {
  "build:chat": "vite build --outDir ../../static/chat"
}
```

`.gitignore` 需同步加 `/static/chat/`（与既有 `/static/dashboard/` 同理）。

### 5.4 实施顺序

1. `web_chat_channel.py` 单用户版 + 5.1 鉴权 + 出站按 2.4 的流式接口 → 功能可跑通
2. 5.2 限流 + 5.3 前端 → 可对外
3. 4.1b 出站 Pub/Sub → 才可多 Worker

第 3 步之前必须在配置中锁 `worker_count = 1`，否则回复会静默丢失。

---

## Phase 6: 配置与监控（P2 - 建议）

### 6.1 动态配置

```toml
# config.toml
[scaling]
max_concurrent_users = 5000
worker_count = 10
message_queue_size = 10000

[llm.rate_limits]
# 多个 API Key 轮询
api_keys = ["sk-1", "sk-2", "sk-3"]
max_rpm_per_key = 50
max_concurrent_requests = 100

[storage.postgres]
pool_size = 50
statement_cache_size = 500
```

### 6.2 监控指标

```python
# infra/monitoring/metrics.py
from prometheus_client import Counter, Histogram

llm_requests = Counter("llm_requests_total", "LLM API calls")
llm_latency = Histogram("llm_latency_seconds", "LLM response time")
queue_depth = Gauge("message_queue_depth", "Pending messages")
```

---

## 改造优先级与成本估算

| 阶段            | 改动点            | 工作量 | 性能提升     | 必要性 |
| ------------- | -------------- | --- | -------- | --- |
| Phase 1.1     | PostgreSQL 迁移  | 2周  | 10x 写入吞吐 | 必须  |
| Phase 1.2     | Redis 缓存       | 1周  | 5x 读取速度  | 必须  |
| Phase 2.1-2.2 | 异步 + 速率限制      | 3周  | 3x 并发处理  | 必须  |
| Phase 2.3     | AgentLoop 异步改造 | 2周  | 2x 资源利用率 | 必须  |
| Phase 2.4     | 出站改流式接口        | +0.5周 | 免除后续返工 | 必须  |
| Phase 3.1     | 向量数据库独立        | 1周  | 5x 检索性能  | 重要  |
| Phase 4.1-4.2 | 消息队列 + 水平扩展    | 2周  | 无限水平扩展   | 重要  |
| Phase 4.1b    | 出站 Pub/Sub     | 1周  | WS 多 Worker 前置 | 必须（若启 webchat）|
| Phase 5.1     | webchat 鉴权     | 1周  | 防数据泄露    | 必须（若启 webchat）|
| Phase 5.2-5.4 | 限流 + channel + 前端 | 2周 | 功能可用 | 必须（若启 webchat）|
| Phase 6       | 监控与配置          | 1周  | 可观测性     | 建议  |

**总计**：约 15 周（不启 webchat 则 12 周）

**排期约束**：

- 5.1 鉴权不依赖存储层，可与 Phase 1 并行
- 5.4 的 channel 实现应在 2.4 接口定型后开始，否则出站要改两遍
- 4.1b 是 webchat 上多 Worker 的硬前置，与 Worker Pool 同批上线

---

## 兼容性策略（渐进式迁移）

### 存储层抽象

```python
# infra/storage/factory.py
def create_store(config: StorageConfig):
    if config.backend == "sqlite":
        return MemoryStore2(config.sqlite_path)
    elif config.backend == "postgres":
        return PostgresMemoryStore(config.postgres_url)
    else:
        raise ValueError(f"Unknown backend: {config.backend}")

# 业务代码无需修改
store = create_store(config.storage)
items = await store.vector_search(query_vec, top_k=8)
```

### 逐步迁移用户

1. **灰度发布**：10% 用户先走新架构
2. **双写验证**：同时写 SQLite + PostgreSQL，对比一致性
3. **全量切换**：验证稳定后，停止 SQLite 写入
4. **清理遗留代码**：移除 `memory2/store.py` 旧实现

---

## 资源需求估算（5000 用户）

### 硬件配置

| 组件                                    | 规格       | 数量   | 用途      |
| ------------------------------------- | -------- | ---- | ------- |
| Gateway 实例 | 2核 4GB | 3台 | 鉴权 + WS 常驻连接（webchat 需要）|
| Worker 实例 | 8核 16GB  | 10台  | 处理用户消息  |
| PostgreSQL                            | 16核 64GB | 1主2从 | 持久化存储   |
| Redis Cluster                         | 8核 32GB  | 3节点  | 缓存 + 队列 + Pub/Sub |
| Qdrant                                | 8核 32GB  | 2节点  | 向量检索    |
| Nginx                                 | 2核 4GB   | 2台   | 负载均衡    |

Gateway 单独列出是因为 5000 常驻 WS 连接的画像与 Worker 完全不同：I/O 密集、内存
占用来自连接态（约 100-250MB）而非模型上下文，2 核即可撑数千连接。混部见 4.1c。

### 成本估算（云服务器）

- **计算**：Worker 10 × $100/月 = $1000；Gateway 3 × $50/月 = $150
- **数据库**：$300/月（托管 PostgreSQL）
- **缓存**：$200/月（托管 Redis）
- **向量数据库**：$400/月（Qdrant Cloud）
- **流量**：$100/月
- **总计**：约 $2150/月（不启 webchat 可省下 Gateway 的 $150，即 $2000/月）

---

## 验证清单（上线前）

- [ ] **负载测试**：模拟 5000 用户并发，QPS > 500
- [ ] **数据一致性**：新旧存储层数据校验
- [ ] **故障恢复**：单点故障不影响服务（Redis/PG 主从切换）
- [ ] **LLM 限流**：确认不会触发 API 封禁
- [ ] **内存监控**：Worker 内存占用 < 4GB
- [ ] **响应时间**：P95 延迟 < 3秒（包含 LLM 调用）

### WebChat 专项（若启用）

- [ ] **端点鉴权**：`/ws`、`/api/chat/sessions`、`/messages`、`/media`、`/uploads` 全部要求 token
- [ ] **越权测试进 CI**：持 A 的 token 请求 B 的会话/消息/附件，断言 403
- [ ] **session_key 不可伪造**：前端传任意 `session_key`，服务端仍按 token 派生
- [ ] **附件归属校验**：`/api/chat/media` 拒绝读取他人会话的文件（非仅路径校验）
- [ ] **启动自检**：`host = "0.0.0.0"` 且 `auth.enabled = false` 时启动失败
- [ ] **多 Worker 出站**：10 Worker 下入站消息数 = 出站送达数
- [ ] **重连补齐**：断开期间的回复可从 REST 拉回（不依赖 Pub/Sub 持久化）
- [ ] **fd 上限**：5000 并发 WS 不触发 `EMFILE`，`ulimit -n` 已调至 65535
- [ ] **心跳存活**：连接空闲 10 分钟不被反代切断
- [ ] **限流位置**：超频请求在入队前被拒，队列深度与 LLM 调用数不增长
- [ ] **流式渲染**：前端逐字显示，末帧 `tool_chain` 正确落地

---

## 长期演进路线

### 10000 用户

- **Kubernetes 部署**：自动伸缩 Worker 数量
- **多区域部署**：按地域分流（国内/国外）
- **Gateway 独立扩缩容**：连接数与计算量解耦后，按在线数而非消息量伸缩 Gateway

### 50000 用户

- **分布式向量检索**：Milvus 或 Weaviate 集群
- **出站通道分片**：单 Redis 实例 Pub/Sub 约万级 channel 上限，改按 `session_key`
  哈希分片到多实例，或换 Redis Streams + consumer group（可回溯、有 ACK）

### 100000+ 用户

- **边缘计算**：CDN 缓存静态内容
- **模型本地化**：自部署 LLM 降低 API 成本
