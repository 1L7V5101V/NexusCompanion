"""
QueryPlanner — 自适应检索路由

两层结构:
1. 规则打分（非互斥遍历，每条规则输出 source + score）
2. 阈值决策 → 单源直出 / light_provider LLM 兜底分类

用法:
    planner = QueryPlanner(light_provider=...)
    result = await planner.classify("今天天气怎么样")
    # PlannerResult(sources=["web"], method="rule", ...)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent.provider import LLMProvider

logger = logging.getLogger(__name__)

# ── 规则正则（集中定义，便于测试和调参） ─────────────────────────

# 问候语: ^...$ 保证纯问候才匹配，排除"你好今天天气"等混合 query
_GREETING_RE = re.compile(
    r"^(你好|在吗|hi|hello|hey|早|早安|早上好|上午好|中午好|下午好"
    r"|晚安|晚上好|嗨|哈喽|嗨喽|yo)[!!。~～]*$",
    re.IGNORECASE,
)

# 实时/时间敏感类
_TIME_RE = re.compile(
    r"(天气|新闻|最新|今天|明天|昨天|现在|当前|实时|直播"
    r"|weather|news|today|\bnow\b|current|latest|update|forecast"
    r"|股票|汇率|油价|金价|疫情|台风|地震)",
    re.IGNORECASE,
)

# 文档/知识类
_DOC_RE = re.compile(
    r"(论文|thesis|paper|文档|doc|documentation|手册|指南"
    r"|代码|源码|source.code|API|函数|class |def |import |语法"
    r"|术语|定义|什么意思|如何用|怎么用|用法|示例|example)",
    re.IGNORECASE,
)

# 关系推理类
_RELATION_RE = re.compile(
    r"(上次|之前|刚才|你说过|你提到|你讲过|以前|回忆|还记得"
    r"|recall|remember|你刚才|前面|上一条|上轮|上一轮"
    r"|how long|known each other|we talked|we discussed|we chatted"
    r"|you told|you mentioned|you said|you told me|you mentioned that"
    r"|do you remember|remember when|remember that|recall when"
    r"|认识|记得.*吗|跟你.*说过|我们聊过)",
    re.IGNORECASE,
)

# ── Planner 输出 ────────────────────────────────────────────


@dataclass
class PlannerResult:
    sources: list[str] = field(default_factory=lambda: ["graph"])
    method: str = "rule"          # "rule" | "llm" | "rule_default"
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""


# ── QueryPlanner ───────────────────────────────────────────


class QueryPlanner:
    """检索路由规划器。

    router_mode:
    - "rule":  规则打分 + 阈值决策，模糊时 LLM 兜底（默认）
    - "llm":   跳过规则打分，直接由 LLM 判断检索源

    规则分值说明:
    - web +0.7: 时间/新闻类关键词区分度高，误判代价低
    - graph +0.5/+0.6: 关系推理和问候语
    - vector +0.6: 文档/代码类
    - graph +0.1: 默认兜底，保证所有 query 至少有一个候选
    """

    def __init__(
        self,
        light_provider: LLMProvider | None = None,
        light_model: str = "",
        router_mode: Literal["rule", "llm"] = "rule",
    ) -> None:
        self._light_provider = light_provider
        self._light_model = light_model
        self._router_mode = router_mode

    async def classify(self, query: str) -> PlannerResult:
        """异步分类入口。

        router_mode="llm" 时跳过规则打分，直接走 LLM 分类；
        否则走规则打分 + 阈值决策（LLM 仅作为模糊区间兜底）。
        """
        if self._router_mode == "llm":
            return await self._llm_direct_classify(query)
        scores = self._score(query)
        return await self._decide(query, scores)

    # ── 规则打分 ──────────────────────────────────────────────

    def _score(self, query: str) -> dict[str, float]:
        scores: dict[str, float] = {"graph": 0.0, "vector": 0.0, "web": 0.0}

        # 问候语 → graph（高置信度，纯问候才匹配 ^...$）
        if _GREETING_RE.search(query.strip()):
            scores["graph"] += 0.6

        # 实时/时间敏感 → web
        if _TIME_RE.search(query):
            scores["web"] += 0.7

        # 文档/知识 → vector
        if _DOC_RE.search(query):
            scores["vector"] += 0.6

        # 关系推理 → graph
        if _RELATION_RE.search(query):
            scores["graph"] += 0.5

        # 默认兜底 → graph（所有 query 都加，保证总有一个最低候选）
        scores["graph"] += 0.1

        return scores

    # ── 决策逻辑 ──────────────────────────────────────────────

    async def _decide(self, query: str, scores: dict[str, float]) -> PlannerResult:
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_source, top_score = sorted_scores[0]
        second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0
        gap = top_score - second_score

        # 1. 最高分 >0.8 且远超第二 → 单源直出（跳过 LLM）
        if top_score >= 0.8 and gap >= 0.3:
            return PlannerResult(
                sources=[top_source], method="rule", scores=scores,
                reason=f"{top_source}={top_score:.1f} >> 第二={second_score:.1f}, 高置信度",
            )

        # 2. 最高分 ≥0.5 且唯一 → 单源
        high_scorers = [s for s, sc in sorted_scores if sc >= 0.5]
        if len(high_scorers) == 1:
            return PlannerResult(
                sources=high_scorers, method="rule", scores=scores,
                reason=f"单源 {high_scorers[0]}={top_score:.1f}",
            )

        # 3. top2 差距 < 0.2 或所有分 < 0.3 → LLM 兜底
        needs_llm = gap < 0.2 or top_score < 0.3
        if needs_llm and self._light_provider is not None:
            return await self._llm_classify(query, scores)

        # 4. 无 LLM 时的降级：选最高分源
        if top_score >= 0.3 and top_source:
            return PlannerResult(
                sources=[top_source], method="rule", scores=scores,
                reason=f"降级(无LLM): {top_source}={top_score:.1f}",
            )

        # 5. 所有分数极低（问候语已被规则2捕获，这里只可能是无任何命中）
        return PlannerResult(
            sources=["graph"], method="rule_default", scores=scores,
            reason="所有源分数 <0.3, 默认 graph",
        )

    # ── LLM 兜底分类 ──────────────────────────────────────────

    async def _llm_classify(self, query: str, scores: dict[str, float]) -> PlannerResult:
        if self._light_provider is None:
            top = max(scores, key=scores.get)  # type: ignore[arg-type]
            return PlannerResult(
                sources=[top], method="rule", scores=scores,
                reason="light_provider 不可用, 规则降级",
            )

        prompt = self._build_llm_prompt(query, scores)
        try:
            response = await self._light_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=self._light_model,
                max_tokens=128,
            )
            content = (response.content or "").strip()
            data = json.loads(content)
            sources = data.get("sources", ["graph"])
            if not isinstance(sources, list) or not sources:
                sources = ["graph"]
            valid = {"graph", "vector", "web"}
            sources = [s for s in sources if s in valid] or ["graph"]
            return PlannerResult(
                sources=sources, method="llm", scores=scores,
                reason=data.get("reason", ""),
            )
        except Exception as e:
            logger.warning("LLM 分类失败, 降级到规则: %s", e)
            top = max(scores, key=scores.get)  # type: ignore[arg-type]
            return PlannerResult(
                sources=[top], method="rule", scores=scores,
                reason=f"LLM 异常({e}), 降级 {top}",
            )

    async def _llm_direct_classify(self, query: str) -> PlannerResult:
        """纯 LLM 路由：跳过规则打分，直接让 LLM 判断检索源。"""
        if self._light_provider is None:
            logger.warning("router_mode=llm 但 light_provider 不可用, 降级到默认 graph")
            return PlannerResult(
                sources=["graph"], method="rule_default",
                reason="light_provider 不可用, 降级 graph",
            )

        prompt = self._build_llm_direct_prompt(query)
        try:
            response = await self._light_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=self._light_model,
                max_tokens=128,
            )
            content = (response.content or "").strip()
            data = json.loads(content)
            sources = data.get("sources", ["graph"])
            if not isinstance(sources, list) or not sources:
                sources = ["graph"]
            valid = {"graph", "vector", "web"}
            sources = [s for s in sources if s in valid] or ["graph"]
            return PlannerResult(
                sources=sources, method="llm", scores={},
                reason=data.get("reason", "LLM 直接路由"),
            )
        except Exception as e:
            logger.warning("LLM 直接路由失败, 降级到默认 graph: %s", e)
            return PlannerResult(
                sources=["graph"], method="rule_default",
                reason=f"LLM 异常({e}), 降级 graph",
            )

    def _build_llm_prompt(self, query: str, scores: dict[str, float]) -> str:
        return f"""你是一个检索路由分类器。判断用户问题需要哪些知识源。

可用知识源:
- graph: 对话上下文、历史关系、人物关联（适合"上次你说""之前提到""你还记得吗"等涉及对话历史的问题）
- vector: 本地知识库、文档、论文、代码（适合查定义、术语、API用法、代码示例等需要精确知识的问题）
- web: 实时互联网信息（适合新闻、天气、最新动态、股票、汇率等需要最新数据的问题）

选择规则:
1. 可以单选或多选
2. 如果问题涉及多方面信息,可以同时选择多个源
3. 如果问题不需要任何外部知识,只需要 graph

用户问题: {query}

当前规则打分: {json.dumps(scores, ensure_ascii=False)}

输出严格JSON格式,不要任何解释或Markdown:
{{"sources": ["graph"], "reason": "简要说明选择理由(一句话)"}}
"""

    @staticmethod
    def _build_llm_direct_prompt(query: str) -> str:
        return f"""你是一个检索路由分类器。判断用户问题需要哪些知识源。

可用知识源:
- graph: 对话上下文、历史关系、人物关联（适合"上次你说""之前提到""你还记得吗"等涉及对话历史的问题）
- vector: 本地知识库、文档、论文、代码（适合查定义、术语、API用法、代码示例等需要精确知识的问题）
- web: 实时互联网信息（适合新闻、天气、最新动态、股票、汇率等需要最新数据的问题）

选择规则:
1. 可以单选或多选
2. 如果问题涉及多方面信息,可以同时选择多个源
3. 如果问题不需要任何外部知识,只需要 graph

用户问题: {query}

输出严格JSON格式,不要任何解释或Markdown:
{{"sources": ["graph"], "reason": "简要说明选择理由(一句话)"}}
"""
