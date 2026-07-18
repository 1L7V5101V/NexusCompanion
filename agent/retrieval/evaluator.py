"""
Evaluator — 检索质量质检节点

用 light_provider 对检索结果做结构化打分，纯数值输出，代码做最终判定。
遵循 CRAG/Self-RAG 论文中 retrieval evaluator 的设计范式:
- 评估者和生成者角色分离（不同模型实例）
- 结构化打分而非自由文本
- 分数可量化、可测评
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.provider import LLMProvider

logger = logging.getLogger(__name__)

_RELEVANCE_THRESHOLD = 0.5
_COMPLETENESS_THRESHOLD = 0.5
_MAX_RETRIES = 3


@dataclass
class EvalResult:
    relevance: float = 0.0
    completeness: float = 0.0
    missing_info: str = ""
    verified: bool = False


class Evaluator:
    """检索质量评估器。

    用法:
        evaluator = Evaluator(light_provider)
        result = await evaluator.evaluate(query, retrieved_block)
        if result.verified:
            # block 合格，注入主上下文
        else:
            # 重试或降级

    判定逻辑（纯代码规则，不依赖 LLM 的 verdict 字段）:
        verified = relevance >= 0.5 AND completeness >= 0.5
    """

    def __init__(self, light_provider: LLMProvider | None = None, light_model: str = "") -> None:
        self._light_provider = light_provider
        self._light_model = light_model

    async def evaluate(
        self,
        query: str,
        block: str,
        router_sources: list[str] | None = None,
        router_reason: str = "",
    ) -> EvalResult:
        """单次评估。返回结构化评分 + verified 判定。

        Args:
            query: 用户原始查询
            block: 检索结果文本
            router_sources: Router 选中的检索源列表，如 ["graph"]。传入后
                            Evaluator 会根据源类型调整评估严格度。
            router_reason: Router 的决策理由，用于给 LLM evaluator 做上下文。
        """
        if not block.strip() or not query.strip():
            return EvalResult(
                relevance=0.0, completeness=0.0,
                missing_info="检索内容为空",
                verified=False,
            )

        if self._light_provider is None:
            logger.warning("light_provider 不可用，跳过 Evaluator 质检")
            return EvalResult(
                relevance=0.0, completeness=0.0,
                missing_info="light_provider 不可用",
                verified=False,
            )

        prompt = self._build_eval_prompt(query, block, router_sources=router_sources, router_reason=router_reason)
        try:
            response = await self._light_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=self._light_model,
                max_tokens=192,
            )
            content = (response.content or "").strip()
            data = json.loads(content)
        except Exception as e:
            logger.warning("Evaluator 打分失败: %s", e)
            return EvalResult(
                relevance=0.0, completeness=0.0,
                missing_info=f"评估异常: {e}",
                verified=False,
            )

        relevance = max(0.0, min(1.0, float(data.get("relevance", 0.0))))
        completeness = max(0.0, min(1.0, float(data.get("completeness", 0.0))))
        missing_info = str(data.get("missing_info", ""))

        verified = relevance >= _RELEVANCE_THRESHOLD and completeness >= _COMPLETENESS_THRESHOLD

        return EvalResult(
            relevance=relevance,
            completeness=completeness,
            missing_info=missing_info,
            verified=verified,
        )

    async def evaluate_with_retry(
        self,
        query: str,
        block_fn,  # Callable[[str], Awaitable[str]] — 接受改写后的 query 返回新 block
    ) -> tuple[EvalResult, str, int]:
        """迭代评估 + query 改写重试，最多 _MAX_RETRIES 轮。

        Args:
            query: 原始用户 query
            block_fn: async(query) -> str, 用改写后的 query 重新检索

        Returns:
            (EvalResult, final_block, retry_count)
        """
        current_query = query
        for retry in range(_MAX_RETRIES):
            block = await block_fn(current_query)

            result = await self.evaluate(current_query, block)
            if result.verified:
                return result, block, retry + 1

            logger.info(
                "检索未通过质检 (第%d轮): rel=%.2f, com=%.2f, missing=%s",
                retry + 1, result.relevance, result.completeness, result.missing_info,
            )

            # 最后一轮不再重试
            if retry >= _MAX_RETRIES - 1:
                return result, block, retry + 1

            # Query Rewrite: 聚焦缺失信息
            current_query = await self._rewrite_query(query, result.missing_info)

        # 不应到达这里，但防御性返回
        return EvalResult(
            relevance=0.0, completeness=0.0,
            missing_info="检索重试耗尽",
            verified=False,
        ), "", _MAX_RETRIES

    async def _rewrite_query(self, original_query: str, missing_info: str) -> str:
        """根据 Evaluator 反馈改写 query，提高下一轮检索命中率。"""
        if not missing_info or self._light_provider is None:
            return original_query

        prompt = (
            f"原始问题: {original_query}\n"
            f"缺失信息: {missing_info}\n\n"
            "请改写为一个更精准的检索查询（只输出改写后的查询文本，不要解释）:"
        )
        try:
            response = await self._light_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=self._light_model,
                max_tokens=128,
            )
            rewritten = (response.content or "").strip()
            return rewritten if rewritten else original_query
        except Exception as e:
            logger.warning("Query Rewrite 失败: %s", e)
            return original_query

    @staticmethod
    def _build_eval_prompt(
        query: str,
        block: str,
        router_sources: list[str] | None = None,
        router_reason: str = "",
    ) -> str:
        # 路由上下文说明：帮助 LLM 按场景调整评估尺度
        router_context = ""
        if router_sources is not None:
            router_context = (
                "\n路由信息:\n"
                f"- 检索源: {', '.join(router_sources)}\n"
                f"- 路由理由: {router_reason or '(未提供)'}\n"
            )
            # graph 对话上下文本就是辅助信息，放宽标准
            if router_sources == ["graph"]:
                router_context += (
                    "\n注意: 对话上下文检索是「有更好，没有也能聊」的辅助信息。\n"
                    "检索内容即使不完全匹配用户当前话题，只要是对同一对话历史中\n"
                    "相关交流片段的记录，就应视为有参考价值。relevance 可适当从宽。\n"
                )

        return f"""你是一个检索质量评估员。评估检索内容是否能回答用户问题。

用户问题: {query}

检索内容:
{block}

评分标准:
- relevance (0.0-1.0): 检索内容与问题的相关度。内容是否直接针对问题?是否答非所问?
- completeness (0.0-1.0): 信息完整度。仅凭这些信息能否回答用户问题?是否存在关键信息缺失?

注意:
- relevance 低但 completeness 高: 内容详实但不相关 → 不合格
- relevance 高但 completeness 低: 内容相关但漏掉了关键信息 → 可能需要补充检索
{router_context}
输出严格JSON格式,不要任何解释或Markdown:
{{"relevance": 0.0, "completeness": 0.0, "missing_info": "简述缺什么"}}
"""
