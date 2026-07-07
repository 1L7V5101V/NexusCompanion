# NexusCompanion

一个会主动找你的 AI 助手——不只被动回答问题，还能根据你订阅的信息源主动判断该不该发消息，空闲时自己干点后台活。

---

## 快速开始

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)（`pip install uv`）。

```bash
git clone <this-repo>
cd NexusCompanion
uv venv && uv pip install -r requirements.txt
```

### 1. 初始化

```bash
uv run python main.py setup    # 交互向导（推荐）
uv run python main.py init     # 非交互，CI 用
```

### 2. 配置 config.toml

推荐 DeepSeek + Qwen 组合：

```toml
[llm]
provider = "deepseek"

[llm.main]
model = "deepseek-v4-flash"
api_key = "sk-..."
base_url = "https://api.deepseek.com/v1"
enable_thinking = true

[llm.fast]
model = "qwen-flash"
api_key = "sk-..."
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

[llm.vl]
model = "qwen-vl-plus"
api_key = "sk-..."
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

[memory.embedding]
model = "text-embedding-v3"
api_key = "sk-..."
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

[channels.telegram]
token = "123456:ABC..."
allow_from = ["your_username"]
```

### 3. 启动

```bash
uv run python main.py
```

给 bot 发条消息就能聊。

> 推荐 DeepSeek V4 Flash + Qwen 系列，其他模型没怎么测过。通信渠道推荐 Telegram。

---

## 它能干什么

### 被动对话

收到消息 → 检索记忆 → 工具调用 → 回复。每轮经过 6 个阶段，插件可以插到任意阶段里。

### 主动推送

定期检查你配的数据源，让 LLM 自己判断有没有必要给你发消息。你刚聊完时它 8 分钟才看一次，半天没动静就 1 分钟看一次。

数据分三种：
- **alert** — 高优先级告警，直接推送
- **content** — 内容流，逐条打分再决定
- **context** — 背景信息，概率注入做兜底

### 空闲任务

没东西可推的时候它不闲着——会执行你写的 SKILL.md，比如审计记忆是不是准确、补用户画像、自我检查。

### 记忆系统

对话结束时会自动提取成结构化事实，存到 MEMORY.md。中间有个 PENDING.md 缓冲区，防止频繁改写破坏 prompt cache。另外有向量数据库做语义搜索。

### 插件系统

插件有 4 种方式介入：

- **PhaseModule** — 在对话的各阶段插逻辑
- **EventBus** — 监听系统事件
- **@on_tool_pre** — 拦截工具调用
- **@tool** — 注册新工具

---

## 通信渠道

| 渠道 | 配置位置 |
|------|---------|
| Telegram | `[channels.telegram]` |
| QQ (NapCat) | `[channels.qq]` |
| QQBot (官方) | `[plugins.qqbot]` |
| 飞书 | `[plugins.feishu]` |
| CLI TUI | `main.py cli` |

每个渠道都有白名单（`allow_from`），不在名单里的人用不了。

---

## 其他命令

```bash
uv run python main.py cli           # 连接运行中的 agent（TUI）
uv run python main.py dashboard     # Web 面板（:2236）
uv run python main.py plugin-install --source <url>
uv run python main.py plugin-doctor <plugin_id>
uv run python main.py --help
uv run python main.py --inspect-modules

pytest tests/
akashic_RUN_SCENARIOS=1 pytest -c pytest-scenarios.ini tests_scenarios/
```

运行时数据在 `~/.akashic/workspace/`。

---

## Docker

```bash
docker compose up -d
docker compose logs -f
```

---

## 社区插件

插件仓库：<https://github.com/orgs/akashic-plugins/repositories>

常用插件：steam-mcp、feed-mcp、huayue-skills

运行中的 agent 可以直接聊天让它装插件：

> "帮我装一下 https://github.com/akashic-plugins/steam-mcp"

---

## 许可证

MIT
