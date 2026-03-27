from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class ProfileFact:
    summary: str
    category: str
    happened_at: str | None


class ProfileFactExtractor:
    def __init__(
        self,
        llm_client: Any,
        *,
        model: str = "",
        max_tokens: int = 400,
        timeout_ms: int = 1200,
    ) -> None:
        self._llm_client = llm_client
        self._model = model
        self._max_tokens = max(128, int(max_tokens))
        self._timeout_s = max(0.1, float(timeout_ms) / 1000.0)

    async def extract(
        self,
        conversation: str,
        *,
        existing_profile: str = "",
    ) -> list[ProfileFact]:
        # 1. 先构造 prompt；空对话直接返回空列表。
        if not str(conversation or "").strip():
            return []
        prompt = self._build_prompt(
            conversation=conversation,
            existing_profile=existing_profile,
        )

        # 2. 再调用 LLM；异常时 fail-open 返回空列表。
        try:
            response = await asyncio.wait_for(
                self._llm_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    model=self._model,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout_s,
            )
        except Exception:
            return []

        # 3. 最后解析 XML 并做去重；乱码时同样返回空列表。
        content = str(getattr(response, "content", response) or "")
        return self._parse_facts(content)

    async def extract_from_exchange(
        self,
        user_msg: str,
        agent_response: str,
        *,
        existing_profile: str = "",
    ) -> list[ProfileFact]:
        """只从单轮 user/assistant 交换中提取 purchase/status/personal_fact。"""
        if not (str(user_msg or "").strip() or str(agent_response or "").strip()):
            return []

        prompt = self._build_exchange_prompt(
            user_msg=user_msg,
            agent_response=agent_response,
            existing_profile=existing_profile,
        )
        try:
            response = await asyncio.wait_for(
                self._llm_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    model=self._model,
                    max_tokens=min(self._max_tokens, 200),
                ),
                timeout=min(self._timeout_s, 0.6),
            )
        except Exception:
            return []

        content = str(getattr(response, "content", response) or "")
        facts = self._parse_facts(content)
        allowed = {"purchase", "status", "personal_fact"}
        return [fact for fact in facts if fact.category in allowed]

    @staticmethod
    def _build_prompt(*, conversation: str, existing_profile: str) -> str:
        return f"""你是 profile 事实提取器。请只从对话里提取用户长期可检索的 profile 事实，并输出 XML。

仅允许以下 5 类：
- purchase：用户购买 / 下单了什么
- decision：用户明确拍板了什么方案 / 计划，或重要宣布（项目公开/上线/重要变更决定）
- preference：用户明确表达的稳定偏好
- status：用户某件事的状态变化（等待 / 完成 / 放弃 / 里程碑达成）
  示例：游戏通关、项目公开、任务完成
- personal_fact：用户关于自身的事实性披露，包括：
  · 身份/背景：职业、居住地、家庭成员、健康状况、技能
  · 持有/拥有数量："我有 N 个 X"（如拥有20个播放列表、养了3只猫、收藏了50张黑胶唱片）
  · 人际关系：谁给了我什么、谁住在哪里、家人/朋友的具体信息

必须遵守：
- 纯技术讨论、闲聊、打招呼，不输出
- 只有当用户在对话中直接陈述自己的事实时，才允许提取
- 用户提问、追问、反问、记忆测试句都不算事实披露，绝对禁止反推成 profile
  示例：
  · "你还记得我什么时候开始戴 fitbit 手环的吗"
  · "我之前是不是买过这个"
  · "你记得我住哪里吗"
  以上都应返回空，不得根据既有上下文或模型猜测补出答案
- 若 existing_profile 已有相同事实，不重复输出
- summary 要简洁、可独立检索
- 每一件具体的事单独一条，绝对不要合并
  ✗ 错误："用户购买了多件商品"
  ✓ 正确：每件商品单独一条，写出具体名称/型号
- 涉及列举时（多件购买、多个决定）每项单独输出
- summary 写出具体内容而非概括：写"用户购买了罗技 MX Master 3 鼠标"而非"用户购买了外设"

【证据源规则】
- ASSISTANT 的回复只作为背景参考，不能作为提取证据
- 即使 ASSISTANT 说"你之前买了 X""你是 XX 方向的学生"，也不得作为事实来源
- 只有 USER 原话中明确陈述的事实才允许提取

【额外禁止类型】
- 工程操作过程：安装依赖、配置环境、调试步骤、更新工具版本
  → 这些是工程 event，不是用户身份/状态的 profile
- 项目内讨论：架构决策、重构方案、代码评审意见
  → 不算用户自身 profile；decision 仅指用户个人/产品层面的重要决定，不含技术实现讨论
- 用户表达的观点 / 意见
  → 必须是关于用户自身的客观事实，而非用户对某事物的看法

当前已有 profile（用于查重）：
{existing_profile or "（空）"}

待处理对话：
{conversation}

只输出 XML：
<facts>
<fact>
  <summary>...</summary>
  <category>purchase|decision|preference|status|personal_fact</category>
  <happened_at>YYYY-MM-DD</happened_at>
</fact>
</facts>"""

    @staticmethod
    def _build_exchange_prompt(
        *,
        user_msg: str,
        agent_response: str,
        existing_profile: str,
    ) -> str:
        return f"""你是单轮 profile 事实提取器。只看这一轮对话（1 条 USER + 1 条 ASSISTANT），不要推断、不要联想。

只允许提取以下 3 类：
- purchase：用户刚购买/下单了什么
- status：用户某件事的状态变化（等待、到货、完成、放弃），或里程碑达成（游戏通关、项目上线/公开、任务完成、竞赛结果）
- personal_fact：用户关于自身的事实性披露，包括身份/背景、持有数量（"我有 N 个 X"）、人际关系（谁给了我什么、家人住在哪里）

禁止输出：
- decision
- preference
- 纯闲聊、打招呼
- 纯技术讨论
- 用户提问、追问、记忆测试句
  例如：
  · "你还记得我什么时候开始戴 fitbit 手环的吗"
  · "你记得我之前为什么发过那张图吗"
  这类句子不是事实披露，必须返回空
- 任何不是用户本人事实的内容
- ASSISTANT 确认或复述的内容，即使涉及用户，也不算用户陈述，不得提取
- 工程操作（安装、更新、配置工具/依赖）不属于 status
  → status 仅指里程碑型状态变化（游戏通关、项目上线、任务完成、竞赛结果）

若 existing_profile 已有同一事实，不重复输出。

提取粒度要求：
- 每一件具体的事单独一条，不要合并
- 写出具体名称/型号/数量，不要用概括性词语
  ✗ 错误："用户购买了游戏外设"
  ✓ 正确："用户购买了罗技 G Pro X 耳机"

当前已有 profile（用于查重）：
{existing_profile or "（空）"}

本轮对话：
USER: {user_msg}
ASSISTANT: {agent_response}

只输出 XML：
<facts>
<fact>
  <summary>...</summary>
  <category>purchase|status|personal_fact</category>
  <happened_at>YYYY-MM-DD</happened_at>
</fact>
</facts>"""

    def _parse_facts(self, raw_output: str) -> list[ProfileFact]:
        allowed = {"purchase", "decision", "preference", "status", "personal_fact"}
        matches = re.findall(r"<fact>\s*(.*?)\s*</fact>", raw_output or "", re.DOTALL)
        facts: list[ProfileFact] = []
        seen: set[tuple[str, str]] = set()
        for block in matches:
            summary = self._extract_tag(block, "summary")
            category = self._extract_tag(block, "category").lower()
            happened_at = self._extract_tag(block, "happened_at") or None
            if not summary or category not in allowed:
                continue
            key = (summary, category)
            if key in seen:
                continue
            seen.add(key)
            facts.append(
                ProfileFact(
                    summary=summary,
                    category=category,
                    happened_at=happened_at,
                )
            )
        return facts

    @staticmethod
    def _extract_tag(raw_output: str, tag: str) -> str:
        match = re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            raw_output or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        return match.group(1).strip() if match else ""
