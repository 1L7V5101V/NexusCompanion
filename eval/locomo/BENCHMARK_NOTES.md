# LoCoMo Benchmark 运行记录

## 概述

LoCoMo（Long Context Memory）基准测试评估 AI Agent 的长期记忆能力。数据集来自 `snap-research/locomo`（CC BY-NC 4.0），包含 10 个多会话对话（共约 800 轮），每个对话有约 200 道 QA 题（总计 1,986 题），涵盖 5 类问题：单跳、时间、多跳、开放域、对抗性。

NexusCompanion 的评测是端到端的：先在 Agent 的记忆系统中回放完整对话（ingest），然后逐题让 Agent 用记忆工具回答，最终用 LLM Judge 判断答案语义是否正确。

---

## 文件结构

```
eval/locomo/
├── BENCHMARK_NOTES.md        # 本文档
├── run.py                    # 主 runner：ingest + QA + 评分
├── dataset.py                # LoCoMo 数据集解析器
├── config.toml               # benchmark 用 LLM 和记忆配置
├── data/locomo10.json        # 下载的原始数据集（10 convs, 1986 QA）
├── results/                  # 聚合结果 JSON
│   ├── 20260720_025636.json               # 首次完整运行（严格 judge，0.0%）
│   ├── 20260720_035112_relaxed_judge.json  # 重评分（宽松 judge，60.3%）
│   └── 20260720_114401_relaxed_judge.json  # 重评分（新 prompt + flash 模型，46.0%）
├── _rejudge.py               # 离线重评分工具
└── _analyze_judge.py         # 假阴性分析工具

eval/longmemeval/             # 共享评测框架
├── metrics.py                # token_f1, exact_match, judge_answer, score_results
├── runtime.py                # BenchmarkRuntime: 生产环境装配（+ _ConsolidationAdapter）
├── ingest.py                 # haystack 回放 + consolidation 管线
├── qa_runner.py              # QA 单题执行逻辑
├── dataset.py                # LMEInstance / LMETurn 数据类（locomo dataset 复用此处的 LMETurn）
├── run.py                    # LongMemEval runner（另一种评测）
└── README.md                 # LongMemEval 文档
```

---

## 问题记录

以下记录运行过程中遇到的问题，不包含结论或修补建议。

### 1. 数据集解析

- 数据集中的 speaker 字段值就是角色名（如 `Caroline`、`Melanie`），而不是固定的 `"speaker_a"` 字符串。首次解析时直接用了 `"speaker_a"` 作为 speaker 判断条件，导致所有轮次被丢弃。
- 修复：从 JSON 的 `conversation.speaker_a` / `conversation.speaker_b` 字段读取实际角色名，再映射到 `user`/`assistant` 角色。

### 2. Consolidation 接口变更

- `ConsolidationService` 类在代码库重构中被移除。consolidation 逻辑移入 `memory_runtime.markdown.maintenance.consolidate()`。
- `runtime.py` 中原有的 `from agent.consolidation import ConsolidationService` 在运行时抛出 ImportError。
- 修复：引入 `_ConsolidationAdapter` 封装新 API。`_ConsolidationAdapter.consolidate(session, archive_all)` 调用 `maintenance.consolidate(ConsolidateRequest(...))`。

### 3. LLM Judge 模型可用性

- 原始评测使用 `dev/deepseek-v4` 作为 Judge 模型，在 `opencode.ai/zen/go/v1` 端点上成功运行。
- 后续运行时该模型返回 401 `"Model dev/deepseek-v4 is not supported"`。测试确认该端点上当前只支持 `deepseek-v4-flash`。
- `deepseek-v4-flash` 需要约 5-60s 每次调用来返回判断结果。API 调用可能超时（需要在代码中加入 `asyncio.wait_for` 超时保护）。
- 不同 judge 模型给出的判断结果不一致，具体差异见下文"评分差异"。

### 4. Thinking 模型答案提取

- Judge 模型启用了 `enable_thinking=true`。某些模型的输出放在 `reasoning_content` 字段而非 `content` 字段。
- 首次评分时 `_verdict_from_message` 只检查 `msg.content`，空字符串导致所有 verdict 为 `None`，最终 judge_acc = 0.0%。
- 修复：在 `content` 为空时检查 `msg.reasoning_content`。

### 5. Judge 调用参数问题

- 对启用了 thinking 的模型，`max_tokens` 需要足够大（>=150-200）让模型在回答之前完成推理。首次重评分时 `max_tokens=50` 导致所有调用返回空，verdict 全部为 None。
- 同样，temperature 设为 0.0 以避免判断随机性。

### 6. 对抗性问题的 gold 答案为空

- LoCoMo 数据集中类别 5（adversarial）的问题，`gold_answer` 为空字符串。这类问题的正确行为是 Agent 否认虚假前提（如 "No record found"）。
- 首次评分时所有 adversarial 问题被判错，因为没有 gold 可对比。
- 后续使用 denial pattern 匹配：如果 Agent 回复包含特定短语（`"no record"`、`"didn't"` 等）则视为正确。

### 7. 日期等价问题

- Gold 答案使用相对日期描述（`"The sunday before 25 May 2023"`、`"The week before 9 June 2023"` 等），而 Agent 倾向于给出绝对日期（`"Saturday, May 20, 2023"`、`"early June 2023"`）。
- 两者在事实上等价，但 LLM Judge（尤其是 flash 模型）经常将这种格式差异判为错误。
- 同样的问题出现在同一事实的不同表述方式：`"7 May 2023"` vs `"May 7, 2023"`。

### 8. 评分差异

不同 Judge 模型和 prompt 得出的评分有显著差异：

| 配置 | Model | Prompt | Judge Acc |
|------|-------|--------|-----------|
| 原始基准 | dev/deepseek-v4 | 严格 | 0.0%（thinking 提取问题） |
| 重评分 #1 | dev/deepseek-v4 | 宽松 + 日期等价规则 | 60.3%（但该模型已不可用） |
| 重评分 #2 | deepseek-v4-flash | 宽松 + 日期等价规则 | 46.0% |

注意 60.3% 和 46.0% 的差异主要来自 Judge 模型本身，而非 Agent 回答质量。

### 9. 假阴性分类

从 46.0% 评分的 90 个假阴性中，观察到的模式：

- **22/90**：日期/时间等价问题（相对日期 vs 绝对日期）
- **68/90**：其他原因，包括：
  - Agent 回答遗漏了 gold 中的部分关键信息
  - Agent 给出了 gold 中没有的额外推断（这些推断可能正确也可能错误）
  - LLM Judge 要求"完全匹配"语义而非"包含核心事实"
  - Agent 回答包含了非关键性的多余信息导致 Judge 认为不一致

---

## 运行方法

### 前置条件

```bash
# 1. 安装依赖
uv pip install -r requirements.txt

# 2. 确保代理可用（访问 opencode.ai 和 dashscope）
# 代理配置: HTTP_PROXY=http://127.0.0.1:7890
```

### 环境变量（用于代理）

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
```

### 数据文件

数据集 `locomo10.json` 已下载到 `eval/locomo/data/locomo10.json`。如重新下载：

```bash
cd eval/locomo/data
wget ...  # 从 snap-research/locomo 获取
```

### 配置

`eval/locomo/config.toml` 包含：
- `[llm.main]`：Agent 和 Judge 共用的 LLM（`deepseek-v4-flash` @ opencode.ai）
- `[memory.embedding]`：嵌入模型（`text-embedding-v4` @ dashscope）
- `[agent]`：Agent 系统提示词、token 限制、工具集
- `[memory.retrieval]`：检索参数（top_k、阈值等）

**注意**：该配置是独立于主项目配置的，修改不影响生产环境。

### 运行完整评测

```powershell
# 全量（10 个 conversation，约 15-20 小时）
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench

# 只跑一个 conversation（如 conv-26）
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench `
  --conversation-idx 0

# 限制每个 conversation 的题数（快速冒烟测试）
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench_smoke `
  --conversation-idx 0 --limit 5
```

### 断点续跑

```powershell
# 自动跳过已完成的 ingest 和 QA
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench `
  --resume-auto
```

### 只跑 ingest（不跑 QA）

```powershell
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench `
  --conversation-idx 0 --ingest-only
```

### 现有结果重评分

当更改了 Judge prompt 或需要重新判断所有答案时，使用独立的重评分脚本（不重新运行 Agent）：

```powershell
uv run python eval/locomo/_rejudge.py
```

该脚本：
- 从 `C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results\` 读取已有的 QA 结果
- 对每个非对抗性问题调用 LLM Judge 重新判断
- 对对抗性问题使用 denial pattern 匹配
- 更新每个文件的 `judge_correct` 字段
- 将聚合结果保存到 `eval/locomo/results/<timestamp>_relaxed_judge.json`

**注意**：该脚本硬编码了 workspace 路径和 conv-26 的结果目录。如需对其他 conversation 重评分，需修改 `results_dir` 路径。

### 查看结果

```powershell
# 结果文件
ls eval/locomo/results/

# 每个 JSON 包含：
# - scores.overall.judge_acc: LLM Judge 准确率
# - scores.overall.f1: Token-level F1
# - scores.overall.em: Exact Match
# - scores.by_type: 按问题类别的细分
# - results[]: 每题详细结果（question, gold_answer, predicted_answer, f1, judge_correct, error, tool_chain）

# 每题 trace 日志：
# /tmp/locomo_bench/<conv-id>/traces/<qa_index>.log
# 包含：Agent 配置、SELF.md、ReAct trace（tool calls 序列）
```

### 分析假阴性

```powershell
uv run python eval/locomo/_analyze_judge.py
```

该脚本读取最新的重评分结果，输出：
- 假阴性总数
- 日期等价候选数（对比 gold 和 predicted 中的日期表述差异）
- 非日期假阴性样本（用于手动分类真实 Agent 错误 vs Judge 判断错误）

---

## 参数说明

### `run.py`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--config` | Path | 必填 | config.toml 路径 |
| `--data` | Path | 必填 | locomo10.json 路径 |
| `--workspace` | Path | `/tmp/locomo_bench` | 工作目录（ingest 状态、QA 结果、trace 日志） |
| `--output` | Path | 自动生成 | 聚合结果 JSON 路径 |
| `--conversation-idx` | int | 全部 | 只处理指定索引的 conversation |
| `--limit` | int | 不限 | 每个 conversation 最多处理的 QA 题数 |
| `--workers` | int | 1 | 并发数（当前 conversation 级别的并发未实现） |
| `--resume-auto` | flag | 否 | 断点续跑：复用已有结果 |
| `--qa-only` | flag | 否 | 跳过 ingest，只跑 QA |
| `--ingest-only` | flag | 否 | 只跑 ingest，跳过 QA |
| `--timeout` | float | 180.0 | 每道题的 Agent 超时（秒） |

### `_rejudge.py`

| 硬编码参数 | 当前值 |
|-----------|--------|
| 结果目录 | `C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results` |
| Judge 模型 | 从 config.toml 读取（当前强制为 `dev/deepseek-v4`，如不可用回退到 config 中的值） |
| API 超时 | 60s |
| `max_tokens` | 150 |
| `temperature` | 0.0 |

---

## 常见问题

### Q: 所有 Judge 结果都是 None

检查：
1. Judge 模型是否支持 thinking？如果支持，`max_tokens` >= 150 且 `temperature` = 0.0
2. `_verdict_from_message` 是否同时检查了 `content` 和 `reasoning_content`

### Q: Agent 回答是中文

配置文件 `[agent] system_prompt` 和 `runtime.py` 中的 `_BENCHMARK_SELF_MD` 都要求 Agent 用英文回答。

### Q: 模型调用超时或返回 401

- 确认 API key 是否有效
- 确认模型名是否在端点上可用（目前已确认 `deepseek-v4-flash` 可用，`dev/deepseek-v4` 已不可用）
- 设置代理环境变量

### Q: 如何扩大评测范围到所有 10 个 conversation？

1. 确认 9 个未运行的 conversation 的 workspace 目录不存在（或使用 `--resume-auto`）
2. 运行 `run.py` 不指定 `--conversation-idx`，将自动遍历所有 conversation
3. 运行时间估算：每个 conv 约 1.5 小时 × 10 = 约 15 小时
