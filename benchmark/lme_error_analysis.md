# LongMemEval 错误分析 (105 题样本)

> 数据来源：`lme_full.log`，105 题，正确率 71.4%（single-session 88.6% / multi-session 37.1%）
> 本文只分析 **single-session** 错误（8 题），multi-session 为聚合计数类结构性缺陷，单独讨论。

---

## 错误分类汇总

| # | QID | 正确答案 | 模型回答 | 根本原因 | 分类 |
|---|-----|---------|---------|---------|------|
| [3] | `51a45a95` | Target | "redeemed…last Sunday, but..." | 事实未被提取存储 | **Extraction miss** |
| [13] | `c960da58` | 20 (Spotify playlists) | "I don't know." | 事实未被提取存储 | **Extraction miss** |
| [18] | `ad7109d1` | 500 Mbps | "1 Gbps" | 正确事实已存储，模型幻觉 | **Model hallucination** |
| [22] | `8ebdbe50` | Data Science (cert) | "food safety cert" | 正确事实在 event，food safety 在 profile 排名更高 | **Retrieval ranking** |
| [40] | `ec81a493` | 500 (limited edition) | "I don't know." | 事实已存储但未被检索到 | **Retrieval miss** |
| [42] | `e01b8e2f` | Hawaii | "I don't know." | Hawaii 只出现在 assistant 推荐文本，未作为用户事实提取 | **Extraction quality** |
| [58] | `1faac195` | Denver | "I don't know." | 事实在 event 里但未被检索到 | **Retrieval miss** |
| [60] | `f4f1d8a4` | my sister | "I don't know." | 原始 transcript 存储，未提炼为可检索事实 | **Extraction quality** |

---

## 逐题详细分析

---

### [3] `51a45a95` — Extraction miss

**问题**：Where did I redeem a $5 coupon on coffee creamer?
**正确答案**：Target
**DB 状态**：DB 中无任何含 "Target" 的事实

**原因**：Consolidation 和 ProfileExtractor 均未将"在 Target 使用优惠券"这一具体门店信息提取出来，可能被合并进了更笼统的购物描述中。

**修复方向**：
- Consolidation prompt 补充指令：商家名称/地点等具体细节必须保留，不能合并
- 或在 ProfileExtractor 的 purchase 类别中加强对"在哪里购买/兑换"的提取

---

### [13] `c960da58` — Extraction miss

**问题**：How many playlists do I have on Spotify?
**正确答案**：20
**DB 状态**：DB 中无 Spotify playlist 数量的记录；"20" 只出现在与 AR 技术完全无关的 event 里（误匹配）

**原因**：用户提到"我有 20 个 Spotify 播放列表"这类个人数量性事实，未被 profile 或 event 提取。可能因为话题在整个 session 中只出现一次且不够显眼。

**修复方向**：
- ProfileExtractor 补充对用户 status 类数量性描述的提取（"我有 N 个…"）
- 此类数量事实优先存为 profile/status 而非 event

---

### [18] `ad7109d1` — Model hallucination

**问题**：What speed is my new internet plan?
**正确答案**：500 Mbps
**DB 状态**：`[profile][active] 用户升级至 500 Mbps 网络速度，且体验良好` ✅ 已正确存储

**原因**：正确事实已被检索到（上下文包含 500 Mbps），但模型回答了 "1 Gbps"。排查路径：
1. 模型可能混入了自身预训练知识（"常见套餐是 1 Gbps"）
2. 或检索返回的 8 条中同时包含路由器相关 event，干扰了模型判断

**修复方向**：
- Answer prompt 加强指令：**只使用记忆中的具体数值回答，不得从通用知识推断**
- 示例负向指令：`"If the memory contains a specific number, use that exact number. Do not substitute with common values."`

---

### [22] `8ebdbe50` — Retrieval ranking

**问题**：What certificate/degree did I recently complete?
**正确答案**：Data Science (certification)
**DB 状态**：
- ✅ Data Science 认证存在于 **event**：`用户计划在LinkedIn上添加最近完成的Data Science认证`
- ❌ Food safety 认证存在于 **profile**：`用户正在准备参加食品安全认证培训`（计划中，非完成）

**原因**：
1. Food safety 是 profile 类型，Data Science 是 event 类型；profile 在语义匹配上占优
2. Food safety 的措辞更像"完成态"（"已确认参加"），而 Data Science 的 event 措辞是"计划添加到 LinkedIn"
3. "完成" 这一关键状态没有被正确归类存储

**修复方向**：
- Consolidation/ProfileExtractor 对"完成/获得/拿到证书"这类 **完成态事实** 应显式存为 profile，而非隐藏在 event 里
- 或 retrieval 阶段对含"完成/获得"关键词的结果加权
- 核心问题：**同一用户有两条互相竞争的相关记忆（food safety vs data science），但只有一个是正确答案**——supersede 在这里没用（两者不矛盾），需要提取时区分"计划"和"完成"

---

### [40] `ec81a493` — Retrieval miss

**问题**：How many copies was the limited edition poster printed in?
**正确答案**：500
**DB 状态**：`[profile][active] 用户拥有一个限量版仅500份的世界级签名海报` ✅ 已正确存储

**原因**：事实已存在，但模型回答 "I don't know."，说明该事实在检索时未被返回（相似度低于 score_threshold 或被 top_k 截断）。

**修复方向**：
- 降低 `score_threshold_event`（当前 0.6）或 `score_threshold_profile`
- 或提高 `top_k`（当前 4 → 8），让更多候选进入上下文
- 根本原因：问题问的是"印了多少份"，而 profile 里存的是"拥有限量版仅500份的海报"，语义偏移导致相似度不够高

---

### [42] `e01b8e2f` — Extraction quality

**问题**：Where did I go for my vacation?
**正确答案**：Hawaii
**DB 状态**：Hawaii 只出现在 event 中，但是 **assistant 的推荐文本**：`用户希望品尝传统夏威夷菜肴如Laulau和Kalua Pig，助理推荐了Helena's Hawaiian Food...`

**原因**：
- 提取器把 assistant 推荐夏威夷餐厅的文本当成了 event 存储
- 用户**本人说去了/要去夏威夷**这一核心事实没有被提炼出来
- 违反了"只提取 USER 说的内容"的规则

**修复方向**：
- Consolidation 和 implicit 提取时需强化"用户的行动/计划/去向"提取
- profile 类的 event 提取要区分：用户自述的目的地 vs assistant 的推荐
- 这是 LME 数据集的特点：用户和 assistant 对话中，地名可能更多出现在 assistant 回复里

---

### [58] `1faac195` — Retrieval miss

**问题**：Where did I visit on my road trip?
**正确答案**：Denver
**DB 状态**：Denver 相关 event 多条：`Seeking kid-friendly attractions in Denver for a visit with sister Emily`，`Planning a family road trip and seeking kid-friendly attractions in various...` ✅ 已存储

**原因**：事实已存储，但模型回答 "I don't know."，检索未返回相关结果。可能原因：
1. 问题问的是"road trip 去哪"，而 event 里是"planning...Denver"，语义漂移
2. 英文原文存储（Denver 的 event 全是英文），检索问题可能也是英文，但相似度打分仍低于阈值

**修复方向**：
- 同 [40]：降低 score_threshold 或提高 top_k
- 额外考虑：对 "planning/trip/visit" 相关问题做关键词增强检索

---

### [60] `f4f1d8a4` — Extraction quality

**问题**：Who gave me the stand mixer?
**正确答案**：my sister
**DB 状态**：`[event] USER: I actually got my new stand mixer as a birthday gift from my sister l...` — 存储的是**原始 transcript 片段**，而非提炼后的事实

**原因**：
- 该信息被存为了原始用户发言（`USER: ...`），而不是结构化的 profile 事实
- "谁给了我什么礼物"这类 **人际关系/礼物来源** 信息未被 ProfileExtractor 识别为 profile 事实
- 检索"who gave me the mixer"很难命中原始 transcript 片段

**修复方向**：
- ProfileExtractor 的 personal_fact 类别应覆盖"X 是谁给的/谁送的"这类人际关系事实
- 或 Consolidation prompt 加强对**礼物来源、人际关系**类细节的提取
- 核心：原始 transcript 不应该直接作为 event 存储，应该提炼为第三人称事实

---

## 问题分类与可优化优先级

| 分类 | 题数 | 可修性 | 优先级 | 方向 |
|------|------|--------|--------|------|
| Retrieval miss（事实有，但没检索到） | 3题 | ⭐⭐⭐ 高 | P0 | 降低 score_threshold / 提高 top_k |
| Extraction miss（事实根本没存） | 2题 | ⭐⭐ 中 | P1 | 优化提取 prompt，补充数量/具体信息 |
| Extraction quality（存了但存的格式不对） | 2题 | ⭐⭐ 中 | P1 | 禁止存原始 transcript，强化第三人称提炼 |
| Model hallucination（有事实但模型不用） | 1题 | ⭐⭐⭐ 高 | P0 | Answer prompt 加强"只用检索到的数值" |
| Retrieval ranking（有事实但排名靠后） | 1题 | ⭐ 低 | P2 | 提取时区分"计划"和"完成"态 |

---

## 快速可验证的改动

```
# config.json 参数调整
score_threshold_event: 0.6 → 0.5   （预计修复 [40][58]，可能帮助 [22]）
top_k: 4 → 8                         （预计修复 [40][58]）

# run_longmemeval.py answer prompt 加一行
"Only use the exact values from the retrieved memories. Do not substitute with common knowledge."
（预计修复 [18]）
```

以上三个改动可在这 8 题上快速 A/B 验证，预期能把 single-session 正确率从 88.6% → 92%+。
