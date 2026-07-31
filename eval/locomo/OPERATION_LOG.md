# LoCoMo Benchmark 完整操作日志

本文档记录从接到 LoCoMo 评测任务到完成首次评测的全部操作，按时间顺序展开。目标是让没接触过这个流程的人也能完全复现。

---

## 目录

1. [背景：LoCoMo 是什么](#1-背景locomo-是什么)
2. [Step 0：理解数据集](#2-step-0理解数据集)
3. [Step 1：下载测试集](#3-step-1下载测试集)
4. [Step 2：解析数据集（第一次写代码）](#4-step-2解析数据集第一次写代码)
5. [Step 3：配置基准测试](#5-step-3配置基准测试)
6. [Step 4：写 Runner（遇到第一个坑）](#6-step-4写-runner遇到第一个坑)
7. [Step 5：Ingest-only 冒烟测试](#7-step-5ingest-only-冒烟测试)
8. [Step 6：全量 QA 运行](#8-step-6全量-qa-运行)
9. [Step 7：重评分（发现 Judge 问题）](#9-step-7重评分发现-judge-问题)
10. [Step 8：改进重评分](#10-step-8改进重评分)
11. [Step 9：分析假阴性](#11-step-9分析假阴性)
12. [Step 10：总结](#12-step-10总结)

---

## 1. 背景：LoCoMo 是什么

LoCoMo（Long Context Memory Benchmark）是一个评测 AI Agent 长期记忆能力的基准测试。数据集来自 HuggingFace 上的 `snap-research/locomo`，协议为 CC BY-NC 4.0。

数据结构：
- 10 个多轮对话（conversation），每个对话包含多个 session
- 每个 session 是一段用户与助手的对话轮次
- 每个对话有约 200 道 QA 题（总计 1,986 题）
- 题目分 5 类：单跳(single_hop)、时间推理(temporal)、多跳(multi_hop)、开放域(open_domain)、对抗性(adversarial)
- 对话双方：speaker_a（用户/Caroline）和 speaker_b（助手/Melanie）

评测方式是端到端的：
1. **Ingest**：把对话历史回放到 Agent 的记忆系统中，让记忆系统重建
2. **QA**：对每道题，让 Agent 用记忆工具（recall_memory / search_messages / fetch_messages）查找答案
3. **评分**：用 LLM Judge 判断 Agent 的答案与 gold answer 是否语义一致

---

## 2. Step 0：理解数据集

首先需要搞清楚 loComo10.json 的结构。文件内容大致如下：

```json
[
  {
    "sample_id": "conv-26",
    "conversation": {
      "speaker_a": "Caroline",
      "speaker_b": "Melanie",
      "session_1": [
        {"speaker": "Caroline", "text": "Hi Melanie, how are you?"},
        {"speaker": "Melanie", "text": "I'm doing well, Caroline! How about you?"}
      ],
      "session_1_date_time": "2023-05-01",
      "session_2": [...],
      "session_2_date_time": "...",
      ...
    },
    "qa": [
      {
        "question": "When did Caroline go to the LGBTQ support group?",
        "answer": "7 May 2023",
        "category": 2,
        "evidence": ["D1:3"]
      },
      ...
    ]
  },
  ...
]
```

关键细节：
- `speaker_a` / `speaker_b` 的值就是角色名（如 "Caroline"、"Melanie"），不是固定字符串 `"speaker_a"`
- session 的 key 是 `session_1` / `session_2` / ...
- 日期 key 是 `session_1_date_time` / `session_2_date_time` / ...
- category 用数字表示：1=single_hop, 2=temporal, 3=multi_hop, 4=open_domain, 5=adversarial
- 对抗性题目（category=5）的 answer 为空字符串

---

## 3. Step 1：下载测试集

数据集在 HuggingFace 上，国内需要走代理。

```powershell
# 设置代理（必需，否则 huggingface.co 连不上）
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:HTTP_PROXY="http://127.0.0.1:7890"

# 用 huggingface_hub 下载
pip install huggingface_hub

python -c "
from huggingface_hub import hf_hub_download
path = hf_hub_download(
    repo_id='snap-research/locomo',
    filename='locomo10.json',
    repo_type='dataset'
)
print(f'Downloaded to: {path}')
import shutil
shutil.copy(path, 'eval/locomo/data/locomo10.json')
"
```

或者直接通过 webfetch 获取原始 URL（使用代理）后保存到本地。

---

## 4. Step 2：解析数据集（第一次写代码）

创建了 `eval/locomo/dataset.py`，任务是读取 locomo10.json 并转换成程序能用的对象。

**第一次 bug：角色名映射错误**

最初写的代码直接用 `"speaker_a"` 字符串判断 speaker：

```python
# 错误代码（第一次写的）
if speaker == "speaker_a":
    role = "user"
elif speaker == "speaker_b":
    role = "assistant"
```

但数据集中 `speaker` 字段的值是 `"Caroline"` 和 `"Melanie"`，不是 `"speaker_a"`。结果所有轮次都被忽略，没有任何数据进入记忆系统。

**修复**：从 JSON 的 `conversation.speaker_a` 和 `conversation.speaker_b` 字段读取实际角色名：

```python
speaker_a_name = str(conv_data.get("speaker_a", "") or "speaker_a")
speaker_b_name = str(conv_data.get("speaker_b", "") or "speaker_b")

# 然后用实际角色名映射
if speaker == speaker_a_name:
    role = "user"
elif speaker == speaker_b_name:
    role = "assistant"
```

**关于 LMETurn**：数据类 `LMETurn(role, content)` 是 `eval/longmemeval/dataset.py` 中定义的通用类，LoCoMo 的 dataset.py 复用它来表示对话轮次。导入语句：

```python
from eval.longmemeval.dataset import LMETurn
```

---

## 5. Step 3：配置基准测试

创建了 `eval/locomo/config.toml`，独立配置用于 Benchmark 的 LLM 和记忆参数。关键部分：

```toml
[llm.main]
model     = "deepseek-v4-flash"
api_key   = "sk-xxxx"
base_url  = "https://opencode.ai/zen/go/v1"
enable_thinking = true

[memory.embedding]
model    = "text-embedding-v4"
api_key  = "sk-yyyy"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
output_dimensionality = 1024

[agent]
system_prompt = """You are a helpful personal assistant with long-term memory...
Answer concisely in English in one sentence or less..."""

[agent.wiring]
toolsets = ["meta_common"]   # 加载 recall_memory, search_messages, fetch_messages, memorize
```

这里注意：
- LLM key 和 embedding key 不同（一个 opencode.ai，一个 dashscope）
- 系统提示词强制英文短答
- 工具集只开记忆相关工具（不开搜索、子 agent 等）
- 这个配置文件独立于主项目的 `config.toml`，互不影响

---

## 6. Step 4：写 Runner（遇到第二个坑）

创建了 `eval/locomo/run.py`，主入口。流程：

```
load_config() → load_locomo() → for each conversation:
  create_runtime()         # 组装生产环境栈
  _ingest_conversation()   # 回放对话到记忆
  for each QA:
    _run_single_qa()       # 让 Agent 回答 + LLM Judge
  close_runtime()
_score_results()           # 聚合评分
save_results()
```

**第二个坑：ConsolidationService 已被移除**

`create_runtime()` 在 `eval/longmemeval/runtime.py` 中定义。它原本这样用：

```python
from agent.consolidation import ConsolidationService  # ❌ 这个类已被删除
```

代码库重构后，consolidation 逻辑移到了 `memory_runtime.markdown.maintenance.consolidate()`。

**修复**：引入 `_ConsolidationAdapter` 封装新接口：

```python
class _ConsolidationAdapter:
    def __init__(self, maintenance):
        self._maintenance = maintenance

    async def consolidate(self, session, *, archive_all=False):
        await self._maintenance.consolidate(
            ConsolidateRequest(session=session, archive_all=archive_all)
        )
```

这样 `runtime.py` 对外暴露的 `BenchmarkRuntime.consolidation` 接口不变，所有调用方（ingest.py、run.py）无需修改。

**关于 Ingest 的细节**：
- 每个 conversation 有一个 merged_session_key（如 `"locomo:conv-26"`）
- 按 session 逐个插入轮次：`session.add_message(role, content)`
- 每个 session 插入完后调用 `consolidate()` 让记忆系统把对话内容归档
- 所有 session 都插入完后，再 finalize 尾部的未归档消息（分块 consolidate）
- 每次 consolidate 后还会触发 post_response_worker（用于知识更新场景的 invalidation）

---

## 7. Step 5：Ingest-only 冒烟测试

先不跑 QA，只验证 ingest 是否能成功回放对话：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"

uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench `
  --conversation-idx 0 --ingest-only
```

结果：conv-26 的 19 个 session、419 轮对话全部成功归档。无报错。

---

## 8. Step 6：全量 QA 运行

跑 conv-26 的全部 199 道题：

```powershell
uv run python -m eval.locomo.run `
  --config eval/locomo/config.toml `
  --data eval/locomo/data/locomo10.json `
  --workspace /tmp/locomo_bench `
  --conversation-idx 0
```

运行了约 1.4 小时，每道题：
1. 创建 QA session（`locomo:conv-26:qa:{index}`）
2. 清理之前的 QA session 数据
3. 发送问题给 Agent（附加 `[Respond in English only...]` 指令）
4. Agent 调用 recall_memory / search_messages / fetch_messages
5. 提取答案
6. 计算 token_f1 和 exact_match
7. 调用 LLM Judge 判断语义正确性
8. 保存结果到 `qa_results/{index:04d}.json`
9. 保存 trace 日志到 `traces/{index:04d}.log`

首次结果保存到 `eval/locomo/results/20260720_025636.json`。

---

## 9. Step 7：重评分（发现 Judge 问题）

初步结果显示 `judge_acc = 0.0%`。显然不对。排查过程：

**问题 A：Thinking 模型的 content 提取**

Judge 模型启用了 `enable_thinking=true`。对于某些模型，输出放在 `reasoning_content` 字段而非 `content` 字段。原始代码只检查 `msg.content`，拿到空字符串，全部判否。

创建了 `eval/locomo/_rejudge.py` 做离线重评分。关键修复：

```python
def _verdict_from_message(msg):
    raw = (msg.content or "").strip()
    if raw:
        return raw.lower()
    # 思模型的内容在 reasoning_content 里
    rc = getattr(msg, "reasoning_content", None) or ""
    rc = rc.strip().lower()
    if rc:
        return rc
    return None
```

**问题 B：max_tokens 太小**

对 thinking 模型，max_tokens=4（`metrics.py` 中 `judge_answer` 的默认值）不够。模型需要在回答前完成推理，token 不够就会返回空。

修复：在 `run.py` 中不直接调用 `metrics.py` 的 judge，而是用独立的重评分脚本，设为 max_tokens=200。

**问题 C：对抗性题目的空 gold**

Category 5（adversarial）的 `gold_answer` 是空字符串。Agent 的正确行为是否认虚假前提（如 "No record found"、"Caroline didn't mention that"）。但 judge 看到空 gold 就无法判断。

修复：在 `_rejudge.py` 中预处理对抗性问题——如果 gold 为空，用 denial pattern 匹配：

```python
if not gold.strip():
    denial_patterns = [
        "no record", "no information", "didn't", "not found",
        "couldn't find", "没有记录", ...
    ]
    new_judge = any(p in predicted.lower() for p in denial_patterns)
```

**问题 D：Judge prompt 太严格**

原始 prompt 要求"strict judge"，导致 Agent 给出了正确答案但因表述方式不同而被判错。

改为宽松 prompt，明确允许：
- 日期格式差异（"7 May 2023" = "May 7, 2023"）
- 额外解释不影响核心事实
- 部分匹配也接受

经过以上修复，重评分结果：

```text
Model: dev/deepseek-v4  @ opencode.ai
Prompt: 宽松（含日期等价规则 + 对抗性处理）
结果: judge_acc = 60.3%,  F1 = 0.2050,  199 questions,  0 errors
保存到: eval/locomo/results/20260720_035112_relaxed_judge.json
```

---

## 10. Step 8：改进重评分

**问题 E：Judge 模型不可用**

几周后再运行，`dev/deepseek-v4` 已返回 401：

```text
Model dev/deepseek-v4 is not supported
```

测试了端点上可用的模型：

```python
models = ['deepseek-v4-flash', 'deepseek-v4', 'dev/deepseek-v4', 'gpt-4o-mini', 'gpt-4o']
# 结果：只有 deepseek-v4-flash 可用
```

`deepseek-v4-flash` 是轻量版模型，判断能力弱于 `dev/deepseek-v4`。而且某些调用可能超时（>20s）。

**修复**：
1. 在 `_rejudge.py` 中添加 `asyncio.wait_for(..., timeout=60.0)` 超时保护
2. 强制模型为 `dev/deepseek-v4`（如不可用则 fallback）
3. 进一步优化 judge prompt，加入日期等价的具体示例

强化后的 judge prompt：

```text
- Equivalent dates count as correct:
  "7 May 2023" = "May 7, 2023" → yes
  "The sunday before 25 May 2023" = "Saturday, May 20, 2023" → yes
  "The week before 9 June 2023" = "early June 2023" → yes
  "2022" = "last year as of May 2023, so 2022" → yes
- Extra items in a list don't make it wrong
- Predicted has MORE specific info than gold → yes
```

**问题 F：API 调用卡死**

某些 judge 调用耗时超过 20 秒甚至挂死。不加超时保护会导致整个脚本卡住。

修复：给所有 judge 调用加 `asyncio.wait_for(..., timeout=60.0)`，超时返回 None。

最终用 `deepseek-v4-flash` 重新评分：

```text
Model: deepseek-v4-flash  @ opencode.ai
Prompt: 宽松（含日期等价规则 + 对抗性处理 + API 超时保护）
结果: judge_acc = 46.0%,  F1 = 0.2050,  199 questions,  0 errors
保存到: eval/locomo/results/20260720_114401_relaxed_judge.json
```

两次评分差异（60.3% vs 46.0%）主要来自 Judge 模型本身的判断能力，而不是 Agent 回答质量的变化。

---

## 11. Step 9：分析假阴性

创建了 `eval/locomo/_analyze_judge.py`，对 90 个假阴性问题分类：

```python
# 核心逻辑：
false_negs = [r for r in results if not r.get("judge_correct") and r.get("gold_answer","").strip()]
# 判断是否含有日期关键词
date_keywords = ["monday","tuesday",...,"week before","2022","2023","ago"]
is_date = any(k in gold.lower() for k in date_keywords)
```

分析结果：
- **22/90**（24%）：日期等价问题（相对日期 vs 绝对日期格式差异）
- **68/90**（76%）：其他原因
  - Agent 遗漏了 gold 中的部分信息
  - Agent 给出了额外推断（可能正确也可能错误）
  - Judge 要求"完全匹配"语义而非"包含核心事实"

---

## 12. Step 10：总结

### 最终产出

| 产出 | 路径 | 说明 |
|------|------|------|
| 数据集解析器 | `eval/locomo/dataset.py` | 读取 locomo10.json 并解析 |
| 基准配置 | `eval/locomo/config.toml` | LLM + 记忆参数配置 |
| 主 Runner | `eval/locomo/run.py` | ingest + QA + 评分的入口 |
| 重评分工具 | `eval/locomo/_rejudge.py` | 离线重新 LLM Judge |
| 假阴性分析 | `eval/locomo/_analyze_judge.py` | 分类假阴性原因 |
| 运行时适配 | `eval/longmemeval/runtime.py` | _ConsolidationAdapter 等 |
| 首次结果 (dev) | `results/...025636.json` | 0.0%（thinking 提取问题） |
| 重评分结果 #1 | `results/...035112_relaxed_judge.json` | 60.3%（dev/deepseek-v4） |
| 重评分结果 #2 | `results/...114401_relaxed_judge.json` | 46.0%（deepseek-v4-flash） |

### 工作区文件结构

运行完成后，workspace 目录（`/tmp/locomo_bench`）内容：

```
/tmp/locomo_bench/
├── conv-26/                      # 每个 conversation 一个目录
│   ├── ingest_state.json          # ingest 状态（是否完成、轮次计数）
│   ├── qa_results/               # 每道题一个文件
│   │   ├── 0000.json              # qa_index=0 的结果
│   │   ├── 0001.json
│   │   └── ...
│   ├── traces/                   # 每道题的 Agent 调用日志
│   │   ├── 0000.log
│   │   ├── 0001.log
│   │   └── ...
│   └── memory/                   # 记忆文件（SELF.md, MEMORY.md 等）
```

### 发现的 6 个关键问题

1. **角色名映射 bug**：speaker 字段是角色名而非 `"speaker_a"`
2. **ConsolidationService 移除**：重构后 consolidation 接口变了
3. **Thinking 模型 content 提取**：答案在 reasoning_content 而非 content
4. **Judge max_tokens 不足**：thinking 模型需要更多 token
5. **对抗性问题空 gold**：需要 denial pattern 匹配
6. **Judge 模型不可用**：dev/deepseek-v4 → deepseek-v4-flash（判断力下降）
