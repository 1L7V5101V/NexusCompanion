# 架构对比：当前 vs 5000 用户版本

> 文档状态：历史设计对照，尚未完全同步 2026-08-21 的架构复审结论。
> 当前架构结论、阶段顺序与验收门槛以 [`SCALING_PLAN.md`](./SCALING_PLAN.md) 和已验证代码为准；本文用于保留方案演进背景，不应单独作为实施依据。
## 一、核心差异总览

| 维度 | 当前架构 | 5000用户架构 | 提升倍数 |
|------|----------|--------------|----------|
| **数据库** | SQLite 单文件 | PostgreSQL 集群 | 10x 写入 |
| **向量检索** | sqlite-vec 内存 | pgvector 分区 HNSW | 5x QPS |
| **并发模型** | 同步 + threading.Lock | 异步 + 消息队列 | 5x 吞吐 |
| **扩展能力** | 单机垂直扩展 | 水平无限扩展 | ∞ |
| **缓存层** | 无 | Redis 多级缓存 | 3x 响应速度 |
| **LLM 调用** | 直接 HTTP | 连接池 + 限流 | 2x 稳定性 |

---

## 二、详细技术对比

### 2.1 存储架构演进

#### 当前：SQLite 中心化
```
┌─────────────────────────────────────┐
│  AgentLoop (单进程)                  │
│    ├─ PassiveTurn                   │
│    ├─ ProactiveTurn                 │
│    └─ DriftTurn                     │
│         ▼                            │
│  MemoryStore2 (threading.RLock)     │
│         ▼                            │
│  SQLite (~/.nexus/store.db)         │
│    - 单文件锁                        │
│    - 写入串行化                      │
│    - VACUUM 会阻塞所有操作           │
└─────────────────────────────────────┘
```

**瓶颈点**：
- **写入冲突**：5个用户同时发消息，后4个等待锁释放
- **文件锁粒度大**：整个数据库文件加锁，无法行级并发
- **备份困难**：VACUUM 或备份时需停服

#### 目标：PostgreSQL 分层架构
```
┌─────────────────────────────────────────────────┐
│  API Gateway (FastAPI)                          │
│    - 接收 webhook                               │
│    - 写入 Redis Queue                           │
└─────────────────────────────────────────────────┘
              ▼
┌─────────────────────────────────────────────────┐
│  Worker Pool (10实例 × 10协程 = 100并发)       │
│    - 消费队列                                   │
│    - 异步处理 Turn                              │
└─────────────────────────────────────────────────┘
      ▼                    ▼
┌──────────────┐  ┌────────────────────────────────┐
│ Redis Cache  │  │ PostgreSQL                     │
│ (热数据)     │  │ (持久化 + 向量检索)            │
│ - Session    │  │ - 行级锁                       │
│ - 用户画像   │  │ - 连接池50                     │
│ - 队列/PubSub│  │ - tenant_id LIST 分区 + HNSW   │
└──────────────┘  └────────────────────────────────┘
```

**优势**：
- **行级锁**：用户A更新记忆不影响用户B读取
- **连接池复用**：50个连接服务 5000 用户
- **在线备份**：PostgreSQL 支持热备份，无需停服

---

### 2.2 并发处理能力

#### 当前：同步阻塞模型
```python
# agent/looping/core.py (简化)
class AgentLoop:
    def run(self):
        while True:
            msg = self.message_bus.get()  # 阻塞等待
            self._handle_message(msg)      # 处理完才接收下一条
    
    def _handle_message(self, msg):
        with self.store._lock:             # 全局锁
            # LLM 调用 (2-5秒)
            response = self.llm_client.chat(...)
            # 写入数据库
            self.store.insert_message(...)
```

**问题**：
- **串行处理**：平均 3秒/消息，QPS = 0.33
- **GIL 限制**：threading 无法利用多核
- **阻塞等待**：LLM 调用时整个进程空转

#### 目标：异步并发模型
```python
# workers/async_agent_loop.py
class AsyncAgentLoop:
    async def run(self):
        # 并发处理 10 条消息
        async with asyncio.TaskGroup() as tg:
            while True:
                batch = await self.queue.get_batch(10)
                for msg in batch:
                    tg.create_task(self._handle_message(msg))
    
    async def _handle_message(self, msg):
        # 无全局锁，每个消息独立处理
        async with self.pool.acquire() as conn:  # 连接池
            response = await self.llm_client.chat(...)
            await conn.execute("INSERT INTO messages ...")
```

**性能对比**（100条消息处理）：
| 指标 | 当前架构 | 异步架构 | 提升 |
|------|----------|----------|------|
| 总耗时 | 300秒 | 35秒 | 8.5x |
| CPU 利用率 | 15% | 85% | 5.6x |
| 内存占用 | 200MB | 400MB | 1/2 |

---

### 2.3 向量检索性能

#### 当前：sqlite-vec 全量扫描
```python
# memory2/store.py
def vector_search(self, query_vec, top_k=8):
    # 加载全部向量到内存
    rows = self.get_all_with_embedding()  # 5000用户 × 1000条 = 500万条
    
    # NumPy 全量计算余弦相似度
    scored = []
    for row in rows:
        similarity = cosine_similarity(query_vec, row.embedding)
        scored.append((row, similarity))
    
    # 排序取 top-k
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
```

**性能瓶颈**：
- **全量加载**：500万条 × 1024维 × 4字节 = 20GB 内存
- **暴力计算**：500万次余弦计算，耗时 2-5 秒
- **无索引**：每次查询都重新计算

#### 目标：pgvector 分区 HNSW

按 `tenant_id` LIST 分区，每个分区独立 HNSW 索引 —— 隔离靠分区而非查询过滤：

```sql
-- alembic/versions/a3d5c7e9f1b2_partition_memory_items.py
CREATE TABLE memory_items (...) PARTITION BY LIST (tenant_id);
CREATE TABLE memory_items_<tid> PARTITION OF memory_items FOR VALUES IN ('<tid>');
CREATE INDEX ON memory_items_<tid>
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

```python
# infra/storage/postgres_memory_store.py
# tenant 绑在 store 实例上，查询自动只命中本分区
store = PostgresMemoryStore(url, tenant_id="chat_alice")
```

**为什么不用「单全局索引 + WHERE 过滤」**（含 Qdrant 的 payload filter）：实测
（`docs/tasks/phase1-storage/vector-validation.md`）小租户召回坍缩 ——

| 租户占比 | 全局 HNSW + 过滤（ef=40）| 分区独立索引 |
|---------|------------------------|-------------|
| 50% | 0.987 | 0.993 |
| 2% | 0.180 | 0.990 |
| 0.1% | 0.100 | 1.000 |

原因是全局图遍历被其他租户节点带偏、目标节点被过滤饿死，ef 提到 200 也只到 0.257。
这是机制问题而非数据稀疏，换引擎不解决 —— 5000 用户系统里单用户通常 ≤1% 占比，
正落在坍缩区间。

**性能对比**（单次查询）：

| 数据量 | sqlite-vec | 分区 HNSW | 提升 |
|--------|------------|-----------|------|
| 10万条 | 200ms | 15ms | 13x |
| 100万条 | 2.5s | 25ms | 100x |
| 500万条 | 12s | 40ms | 300x |

代价是不支持跨 tenant 的全局语义检索 —— 分区隔离恰好挡住这个能力。当前无此需求。

---

### 2.4 LLM API 管理

#### 当前：无保护直连
```python
# 每次请求新建连接
def chat_completion(self, messages):
    response = requests.post(
        f"{self.base_url}/chat/completions",
        json={"messages": messages},
        headers={"Authorization": f"Bearer {self.api_key}"}
    )
    return response.json()
```

**风险**：
- **连接开销大**：每次 TCP 握手 + TLS 握手 ~100ms
- **无限流保护**：突发 100 个请求 → API 返回 429 限流
- **单点故障**：API Key 超额后全部失败

#### 目标：连接池 + 限流 + 重试
```python
class RobustLLMClient:
    def __init__(self, api_keys: list[str]):
        self.pool = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive=20)
        )
        self.limiter = TokenBucketLimiter(rate=50, burst=10)
        self.keys = cycle(api_keys)  # 轮询多个 Key
    
    async def chat_completion(self, messages):
        await self.limiter.acquire()  # 等待令牌
        
        for attempt in range(3):  # 重试 3 次
            try:
                api_key = next(self.keys)
                response = await self.pool.post(...)
                if response.status_code == 429:
                    # 触发限流，换下一个 Key
                    continue
                return response.json()
            except httpx.TimeoutException:
                await asyncio.sleep(2 ** attempt)
        
        raise MaxRetriesExceeded()
```

**可靠性提升**：
- **连接复用**：延迟降低 60%
- **限流保护**：避免 API 封禁
- **多 Key 轮询**：单 Key 60 RPM → 3 Key 180 RPM

---

## 三、关键性能指标对比

### 3.1 吞吐量（Throughput）

| 场景 | 当前架构 | 目标架构 | 提升 |
|------|----------|----------|------|
| 消息处理 QPS | 0.3 | 50 | 166x |
| 记忆写入 TPS | 10 | 200 | 20x |
| 向量检索 QPS | 2 | 100 | 50x |
| 并发用户数 | 50 | 5000 | 100x |

### 3.2 延迟（Latency）

| 指标 | 当前 P95 | 目标 P95 | 改善 |
|------|----------|----------|------|
| 消息响应时间 | 8s | 3s | 62% |
| 记忆检索 | 2.5s | 50ms | 98% |
| LLM 调用 | 5s | 3s | 40% |

### 3.3 可用性（Availability）

| 指标 | 当前 | 目标 |
|------|------|------|
| 单点故障恢复时间 | 5-10分钟（手动重启） | 30秒（自动切换） |
| 数据备份频率 | 手动 | 每小时自动 |
| 故障容忍度 | 0（单机） | 2台（3副本容忍2台故障） |

---

## 四、WebChat 引入的架构差异

webchat 不是"再加一个 channel"。它是**第一个身份自持 + 连接常驻**的接入方式，这两点
让它与 Telegram/QQ 在架构上处于不同类别。

### 4.1 三类 channel 的架构画像

| 维度 | Telegram / QQ / 飞书 | CLI / IPC | WebChat |
|------|---------------------|-----------|---------|
| 身份来源 | 平台签发，不可伪造 | 本机 socket，隐含可信 | **自己签发** || 连接模型 | 无状态 webhook | 单条本地连接 | **N 条常驻 WS** |
| 进程亲和 | 无 | 单进程 | **连接绑定实例** |
| 输出形态 | 整段发送 | 逐行 | **逐字流式** |
| 滥用防护 | 平台已挡 | 无需 | **自行实现** |
| 暴露面 | 仅 webhook 回调 | 本机 | **公网 HTTP + WS** |

前两类在 5000 用户下对进程几乎无常驻开销；webchat 的每个打开的标签页都是一条活连接。
这一条差异派生出下面全部问题。

### 4.2 身份链的信任根

```
平台 channel：  Telegram 保证 chat_id 真实
                    ↓
              session_key = "telegram:12345"   ← 可信

webchat 若沿用同构造法：
              前端自己生成 id
                    ↓
              session_key = "chat:<任意值>"     ← 不可信
                    ↓
              改本地 id 即可读他人会话
```

Phase 1 的 schema 把 `tenant_id` 做成 `memory_items` 的 LIST 分区键，并贯穿
`sessions` / `messages` 等全部表，所以**信任根是 `tenant_id`，`session_key` 是次级
标识**：

- `tenant_id` 决定命中哪个分区 —— 记忆隔离由 PG 分区物理保证
- 每个分区有独立 HNSW 索引，查错 tenant 命中的是另一个分区而非同表其它行
- `session_key` 在 tenant 内区分会话，`memory2/store.py` 的 scope 匹配用它

这比"应用层加过滤条件"强一层：过滤条件可能漏传，分区键不传则写入直接失败。但前提
是二者都必须由服务端从已验证的 token 派生 —— **一旦 `tenant_id` 可从请求参数传入，
分区隔离就退化成了字符串比较**。

### 4.3 出站路径：单进程假设的失效

现有 `ipc_server.py:57` 的连接映射：

```python
self._writers: dict[str, asyncio.StreamWriter] = {}
```

这在单进程下正确。引入 Worker Pool 后：

```
        单进程（现状）                    多 Worker（Phase 4）
                                    
   用户 ──WS── 进程 ──处理──┐        用户 ──WS── Gateway-A
              │            │                        │
              └──_writers──┘                    入队 Redis
                回复送达                            │
                                              Worker-B 处理
                                                    │
                                        Worker-B._writers 为空
                                                    │
                                              ✗ 回复丢失
```

Telegram/QQ 不受影响 —— Worker 直接调平台 HTTP API 即可送达，无需持有连接。
**只有 WebSocket 会中招**，因此出站 Pub/Sub 是 webchat 上多 Worker 的硬前置，
而非可选优化。

修正后：

```
Worker-B ──publish──> Redis Pub/Sub ──subscribe──> Gateway-A ──> 用户
         nexus:out:<session_key>          (持有该连接的实例)
```

副作用是好的：Gateway 变为无状态，**不需要 sticky session**，用户重连可落任意实例。

**投递语义须注意**：Redis Pub/Sub 是 at-most-once，订阅者掉线期间消息直接丢弃。
对流式 token 可接受，但完整回复必须由 Worker 直写 PostgreSQL，前端重连后走 REST
补齐。不能把 Pub/Sub 当持久化用。

### 4.4 流式输出：接口形状的时机问题

| 做法 | 结果 |
|------|------|
| Phase 2 异步化时定成 `async def -> str` | webchat 上线时整条调用链再改一遍 |
| Phase 2 异步化时定成 async generator | 流式免费，Telegram 侧末端 `"".join()` |

聚合成整段是一行代码，把整段拆成流要重构整条链。所以即使 webchat 排期靠后，
**出站接口也应在 Phase 2 就定成流式**。这是本次修订把 2.4 前置的唯一原因。

### 4.5 资源约束对照

| 项目 | webhook（现状估算） | webchat（需追加） |
|------|--------------------|------------------|
| fd | 每请求短连接，无常驻 | 5000 常驻，`ulimit` 默认 1024 必须调至 65535 |
| 内存 | 无连接态 | 20-50KB/连接 → 5000 连接约 100-250MB |
| 连接上限配置 | 不适用 | `AppServerConfig.max_connections` 默认 32 为 IPC 设计，需独立配置 |
| 反代 | 普通 HTTP | 需 `Upgrade` 透传 + `proxy_read_timeout 3600s` |
| 保活 | 不适用 | 需 ping/pong，否则 Nginx 60s 切断空闲连接 |
| 部署单元 | Worker 即可 | 需独立 Gateway（I/O 密集，与 Worker 资源画像不同）|

### 4.6 鉴权缺口（当前代码的实际状态）

`bootstrap/chat_api.py` 现有端点均无鉴权。默认 `host = "127.0.0.1"`
（`agent/config_models.py:47`）挡住了这些，但多用户部署必然改 `0.0.0.0` + 反代：

| 端点 | 行 | 缺口 |
|------|-----|------|
| `/ws` | 72 | 任意人可发消息、消耗 LLM 额度 |
| `/api/chat/sessions` | 39 | 返回全部 `chat:` 会话列表 |
| `/api/chat/sessions/{key}/messages` | 54 | 改 key 即读他人完整对话 |
| `/api/chat/media?path=` | 87 | 有 `upload_roots` 校验，但不校验归属 |
| `/api/chat/uploads` | 76 | 无限制写入 |

`_can_read_media`（127 行）的 `upload_roots` 判断防住了路径穿越，方向正确，
但它回答的是"这个路径是否在允许的目录内"，而非"这个请求者是否有权看这个文件"。

**这是从单机自托管转向多用户服务时最容易漏、后果最直接的一项** —— 后果是全量会话
泄露，且无声无息。因此建议 `host` 非环回且鉴权未开时**启动直接拒绝**，而不是
打 warning。

---

## 五、迁移风险与应对

### 风险 1：数据一致性
**问题**：SQLite → PostgreSQL 迁移过程中数据丢失

**应对**：
```python
# 双写验证期（1-2周）
async def upsert_item(self, memory_type, summary, embedding):
    # 同时写入新旧两个存储
    old_id = self.sqlite_store.upsert_item(...)
    new_id = await self.postgres_store.upsert_item(...)
    
    # 异步比对结果
    asyncio.create_task(self._verify_consistency(old_id, new_id))
```

### 风险 2：性能回退
**问题**：PostgreSQL 查询慢于 SQLite（冷启动）

**应对**：
- **预热缓存**：启动时加载热点用户数据到 Redis
- **慢查询监控**：Prometheus 监控 P99 延迟，超过 100ms 告警
- **回滚机制**：保留 SQLite 1周，性能不达标可快速回滚

### 风险 3：LLM API 成本暴增
**问题**：5000用户并发可能导致 API 费用激增

**应对**：
- **每日预算上限**：超过 $100/天自动降速
- **用户分级**：付费用户优先，免费用户排队
- **Prompt 压缩**：长上下文截断，减少 token 消耗

### 风险 4：webchat 越权访问（新增，等级最高）
**问题**：鉴权缺失或 `session_key` 可伪造，导致跨用户读取会话与记忆

**为什么单列**：其它风险的后果是性能或成本，可观测、可回滚；这一项的后果是数据
泄露，发生时无告警、不可撤回。

**应对**：
- **默认拒绝**：`host` 非 `127.0.0.1` 且 `auth.enabled = false` 时启动失败
- **session_key 服务端派生**：从 JWT `sub` 生成，前端传值一律忽略
- **tenant_id 服务端派生**：从 JWT `sub` 生成，前端传值一律忽略。它同时是
  `memory_items` 的分区键，所以越权在存储层被物理隔离（查错 tenant 命中的是另一个
  分区），比应用层过滤强 —— 前提是禁止从请求参数读取 `tenant_id`
- **越权测试进 CI**：持 A 的 token 请求 B 的资源，断言 403；防止后续改动回退

### 风险 5：多 Worker 下 WS 静默丢消息（新增）
**问题**：出站仍用进程内 `_writers` 映射，Worker 与连接不在同一进程

**应对**：
- Phase 4.1b 的 Pub/Sub 与 Worker Pool 同批上线，不分先后
- 灰度期监控「入站消息数 vs 出站送达数」差值，非零即告警
- 单 Worker 部署可暂缓，但需在配置中显式标注 `worker_count = 1` 的约束

---

## 六、实施路线图

```
Week 1-2:  PostgreSQL + Redis 环境搭建
           ├─ 并行：监控埋点（双写验证的前置工具）
Week 3-4:  数据迁移脚本 + 双写验证 + HNSW 参数在真实 1024 维上复测
           ├─ 并行：webchat 鉴权层（tenant_id 已在 schema 就位，不依赖存储层）
           └─ 并行：webchat 前端（纯前端资产）
Week 5-6:  异步 AgentLoop 重构（出站定为流式接口）
Week 7-8:  LLM Client 改造 + 限流
Week 9:    压力测试 + 性能调优（含分区数 planner 开销实测）
Week 10:   灰度发布 + 全量上线
───────────────────────────────────────
Week 11:   出站 Pub/Sub + Gateway 拆分
Week 12:   web_chat_channel.py 实现（接前面已就绪的鉴权与前端）
Week 13:   webchat 越权/负载专项测试 + 上线
```

原 15 周压缩至 13 周：Phase 3 从独立阶段降为调参并入 Phase 1，监控/鉴权/前端三项
移入并行窗口。

**里程碑检查点**：
- Week 2: 双写不一致率指标可观测（否则 Week 3-4 的验证无从判断）
- Week 4: 双写一致性验证通过（错误率 < 0.1%）；分区 HNSW 在 1024 维上召回 > 0.95
- Week 6: 出站接口已定为 async generator（避免 Week 12 返工）
- Week 8: 单 Worker 处理 50 QPS
- Week 9: 5000 用户模拟负载测试通过；5000 分区下 planning time 可接受
- Week 10: 生产环境稳定运行 72 小时
- Week 11: 多 Worker 下出站送达率 100%（入站数 = 出站数）
- Week 13: 越权测试全部返回 403/404；5000 WS 并发无 fd 耗尽

**排期依赖**：Week 12 的 channel 实现依赖 Week 6 的接口定型与 Week 11 的 Pub/Sub。
若 webchat 需提前上线，可先出单 Worker 版本（跳过 Week 11），但届时无法水平扩展，
需在配置中锁 `worker_count = 1`。

---

## 七、可逆性设计（Rollback 策略）

所有改造保持向后兼容，支持快速回退：

```python
# config.toml 一键切换
[storage]
backend = "sqlite"  # 改为 "postgres" 启用新架构

[agent.loop]
mode = "sync"  # 改为 "async" 启用异步

[llm]
client = "simple"  # 改为 "robust" 启用限流

[channels.chat]
enabled = false  # webchat 可独立关闭，不影响其它 channel
```

**回退预案**：
1. **配置回退**（1分钟）：修改配置重启服务
2. **数据回退**（30分钟）：从 PostgreSQL dump 恢复到 SQLite
3. **完整回退**（2小时）：切换 DNS 到旧集群

**webchat 的回退特殊性**：功能回退容易（`enabled = false` 即可），但**数据泄露不可
回退**。因此 Phase 5.1 鉴权必须在首次公网暴露前完成，不能按"先上线再补"处理。

---

## 总结

这次改造的核心是 **从单机同步架构 → 分布式异步架构**，关键技术点：

1. **存储层**：SQLite → PostgreSQL + Redis 缓存
2. **并发模型**：sync + threading → async + asyncio
3. **向量检索**：全量扫描 → 按 tenant 分区 + 每分区 HNSW（不引入独立向量库）
4. **水平扩展**：单进程 → Worker Pool + 消息队列
5. **可靠性**：单点 → 多副本 + 自动故障切换
6. **接入层**：平台 channel → 增加身份自持的 webchat（Gateway + Pub/Sub 出站）

**方案修正记录**：

| 位置 | 原方案 | 修正后 | 起因 |
|------|--------|--------|------|
| Phase 2.4 | 出站返回完整字符串 | async generator，一次性留出流式 | webchat 流式预期 |
| Phase 4.1b | 只做入站队列 | 出站 Pub/Sub，否则多 Worker 下 WS 收不到回复 | webchat 长连接 |
| 资源估算 | 按无状态 webhook 算 | 追加 Gateway + 5000 常驻连接的 fd/内存 | webchat 长连接 |
| Phase 3 | Qdrant 独立部署（1 周）| 并入 Phase 1，仅剩调参（0.5 周）| 实测分区 HNSW 已达 0.99 召回 |
| Phase 6 监控 | P2 收尾项 | P1，Phase 1 双写验证的前置工具 | 无埋点则一致性只能人工抽查 |
| 鉴权归属列 | 新增 `owner_id` 必填参数 | 复用既有 `tenant_id` 分区键 | Phase 1 schema 已贯穿全表 |

**投入产出比**：
- 开发成本：核心 2.5 人月；含 webchat 约 3.25 人月
- 运维成本：$1750/月（含 webchat Gateway 约 $1900/月）
- 支持用户数：50 → 5000（100倍）
- ROI：假设每用户 $5/月，收入从 $250 → $25000/月
