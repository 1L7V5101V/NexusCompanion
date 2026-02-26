import asyncio
import json
import json_repair
import logging
from datetime import datetime
from pathlib import Path

from agent.context import ContextBuilder
from agent.memory import MemoryStore
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from agent.provider import ContentSafetyError, LLMProvider
from agent.tools.registry import ToolRegistry
from session.manager import SessionManager
from proactive.presence import PresenceStore

# 安全拦截时递减历史窗口的倍率序列：全量 → 减半 → 清空
_SAFETY_RETRY_RATIOS = (1.0, 0.5, 0.0)

logger = logging.getLogger(__name__)

# 内部注入的反思提示，不应持久化到 session
_REFLECT_PROMPT = "根据上述工具执行结果，决定下一步操作。"


class AgentLoop:
    """
    主循环：从 MessageBus 消费 InboundMessage，
    驱动 LLM + 工具调用，将结果发回 MessageBus。
    对话历史按 session_key 独立维护，格式为 OpenAI messages。
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        tools: ToolRegistry,
        session_manager: SessionManager,
        workspace: Path,
        model: str = "deepseek-chat",
        max_iterations: int = 10,
        max_tokens: int = 8192,
        memory_window: int = 40,
        presence: PresenceStore | None = None,
        light_model: str = "",
        light_provider: LLMProvider | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.tools = tools
        self.session_manager = session_manager
        self.workspace = workspace
        self.context = ContextBuilder(workspace)
        self.model = model
        # light_model / light_provider 用于 self-check 等辅助推理
        # 留空则退化到主模型/主 provider
        self.light_model = light_model or model
        self.light_provider = light_provider or provider
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self._presence = presence
        self._running = False
        self._consolidating: set[str] = set()  # 正在后台压缩的 session key

    async def run(self) -> None:
        self._running = True
        logger.info(
            f"AgentLoop 启动  model={self.model}  max_iter={self.max_iterations}"
        )
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                try:
                    response = await self._process(msg)
                    await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"处理消息出错: {e}", exc_info=True)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"出错：{e}",
                        )
                    )
            except asyncio.TimeoutError:
                continue

    async def _run_with_safety_retry(
        self, msg: InboundMessage, session
    ) -> tuple[str, list[str], list[dict]]:
        """递减历史窗口重试，处理 LLM 安全拦截错误。

        重试顺序：全量历史 → 减半 → 无历史。
        降级成功后同步修剪 session，防止下次继续触发。
        所有窗口均失败时说明当前消息本身违规，返回友好提示。
        """
        for attempt, ratio in enumerate(_SAFETY_RETRY_RATIOS):
            window = int(self.memory_window * ratio)
            initial_messages = self.context.build_messages(
                history=session.get_history(max_messages=window),
                current_message=msg.content,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_timestamp=msg.timestamp,
            )
            try:
                result = await self._run_agent_loop(initial_messages)
                if attempt > 0:
                    # 降级后成功：修剪 session，避免违规内容继续存在于历史
                    logger.warning(
                        f"安全拦截后以 window={window} 成功，修剪 session 历史"
                    )
                    if window == 0:
                        session.messages.clear()
                    else:
                        session.messages = session.messages[-window:]
                    session.last_consolidated = 0
                    self.session_manager.save(session)
                return result
            except ContentSafetyError:
                if attempt < len(_SAFETY_RETRY_RATIOS) - 1:
                    next_window = int(
                        self.memory_window * _SAFETY_RETRY_RATIOS[attempt + 1]
                    )
                    logger.warning(
                        f"安全拦截 (attempt={attempt + 1})，"
                        f"缩小历史窗口重试 {window} → {next_window}"
                    )
                else:
                    logger.warning("安全拦截：所有窗口均失败，当前消息本身可能违规")
                    return "你的消息触发了安全审查，无法处理。", [], []

        return "（安全重试异常）", [], []

    def stop(self) -> None:
        self._running = False
        logger.info("AgentLoop 停止")

    def _set_tool_context(self, channel: str, chat_id: str) -> None:
        """将当前会话的 channel/chat_id 注入工具，供主动推送时使用。"""
        self.tools.set_context(channel=channel, chat_id=chat_id)

    # ── 私有方法 ──────────────────────────────────────────────────

    async def _process(
        self, msg: InboundMessage, session_key: str | None = None
    ) -> OutboundMessage:
        preview = msg.content[:60] + "..." if len(msg.content) > 60 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender}: {preview}")

        key = session_key or msg.session_key
        session = self.session_manager.get_or_create(key)

        # 超过记忆窗口时后台压缩（不阻塞当前消息处理）
        if (
            len(session.messages) > self.memory_window
            and key not in self._consolidating
        ):
            self._consolidating.add(key)
            asyncio.create_task(self._consolidate_memory_bg(session, key))

        self._set_tool_context(msg.channel, msg.chat_id)
        final_content, tools_used, tool_chain = await self._run_with_safety_retry(
            msg, session
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Self-Check Pass：始终验证回复中的事实声明
        final_content = await self._self_check(final_content, tool_chain)

        preview = (
            final_content[:120] + "..." if len(final_content) > 120 else final_content
        )
        logger.info(f"Response to {msg.channel}:{msg.sender}: {preview}")

        if self._presence:
            self._presence.record_user_message(key)
        session.add_message("user", msg.content)
        session.add_message(
            "assistant",
            final_content,
            tools_used=tools_used if tools_used else None,
            tool_chain=tool_chain if tool_chain else None,
        )
        self.session_manager.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata={
                **(
                    msg.metadata or {}
                ),  # Pass through for channel-specific needs (e.g. Slack thread_ts)
                "tools_used": tools_used,
                "tool_chain": tool_chain,
            },
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
    ) -> tuple[str, list[str], list[dict]]:
        """迭代调用 LLM，直到无工具调用或达到上限。返回 (final_content, tools_used, tool_chain)

        tool_chain 是按迭代分组的工具调用记录，每个元素：
          {"text": str|None, "calls": [{"call_id", "name", "arguments", "result"}]}
        """
        messages = initial_messages
        tools_used: list[str] = []
        tool_chain: list[dict] = []

        for iteration in range(self.max_iterations):
            logger.debug(f"LLM 调用  iteration={iteration + 1}")
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_schemas(),
                model=self.model,
                max_tokens=self.max_tokens,
            )

            if response.tool_calls:
                logger.info(
                    f"LLM 请求调用 {len(response.tool_calls)} 个工具: "
                    f"{[tc.name for tc in response.tool_calls]}"
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(
                                        tc.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for tc in response.tool_calls
                        ],
                    }
                )
                iter_calls: list[dict] = []
                for tc in response.tool_calls:
                    tools_used.append(tc.name)
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info(f"  → 工具 {tc.name}  参数: {args_str[:120]}")
                    result = await self.tools.execute(tc.name, tc.arguments)
                    result_preview = result[:80] + "..." if len(result) > 80 else result
                    logger.info(f"  ← 工具 {tc.name}  结果: {result_preview!r}")
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )
                    iter_calls.append(
                        {
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "result": result,
                        }
                    )
                tool_chain.append({"text": response.content, "calls": iter_calls})

                # 工具结果注入后，提示 LLM 反思并决定下一步
                messages.append({"role": "user", "content": _REFLECT_PROMPT})
            else:
                logger.info(f"LLM 返回最终回复  iteration={iteration + 1}")
                messages.append({"role": "assistant", "content": response.content})
                return response.content or "（无响应）", tools_used, tool_chain

        logger.warning(f"已达到最大迭代次数 {self.max_iterations}")
        return "（已达到最大迭代次数）", tools_used, tool_chain

    async def _self_check(self, response: str, tool_chain: list[dict]) -> str:
        """Self-Check Pass：核查回复中的事实声明是否有工具结果或常识支撑，修正无依据的断言。"""
        _MAX_RESULT = 600
        lines: list[str] = []
        for iter_group in tool_chain:
            for call in iter_group.get("calls", []):
                name = call.get("name", "tool")
                result = call.get("result", "")
                if len(result) > _MAX_RESULT:
                    result = result[:_MAX_RESULT] + "…（已截断）"
                lines.append(f"[{name}]\n{result}")
        tool_evidence = "\n\n".join(lines)

        if tool_evidence:
            evidence_block = f"【工具结果】（可信事实来源）：\n{tool_evidence}"
            no_tool_rule = ""
        else:
            evidence_block = "【工具结果】：（本次无工具调用，无任何外部事实来源）"
            no_tool_rule = (
                "\n**特别规则（无工具调用时）**：回复中出现的具体数字、"
                "产品名称/型号、价格、版本号、发布状态、事件结论、人物动态等，"
                '凡是需要查证才能确认的，一律改为"我不确定"或删除。'
                "无需查证的常识不改。"
            )

        prompt = f"""你是事实核查助手，核查下方【回复】中的具体事实声明是否有可信来源支撑。

规则（严格执行）：
1. 有【工具结果】明确支撑的内容 → 原样保留
2. 具体数字、名称、版本、价格、状态、时间、事件结论若无工具结果支撑 → 改为"我不确定"或删除{no_tool_rule}
3. 一般性措辞、问候、建议语气、无需查证的常识 → 不改
4. 不添加任何新信息
5. 输出与原回复完全相同的语言和风格，不加任何解释或注释

{evidence_block}

【待核查的回复】：
{response}

直接输出核查后的回复正文："""

        try:
            checked = await self.light_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=self.light_model,
                max_tokens=self.max_tokens,
            )
            corrected = (checked.content or "").strip()
            if corrected:
                return corrected
        except Exception as e:
            logger.warning(f"Self-Check Pass 失败，返回原始回复: {e}")
        return response

    async def _consolidate_memory_bg(self, session, key: str) -> None:
        """后台异步压缩，完成后持久化 last_consolidated 并释放锁。"""
        try:
            await self._consolidate_memory(session)
            self.session_manager.save(session)
        finally:
            self._consolidating.discard(key)

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md.

        Args:
            archive_all: If True, clear all messages and reset session (for /new command).
                       If False, only write to files without modifying session.
        """

        memory = MemoryStore(self.workspace)
        if archive_all:
            old_messages = list(session.messages)
            keep_count = 0
            logger.info(
                f"Memory consolidation (archive_all): {len(session.messages)} total messages archived"
            )
        else:
            keep_count = self.memory_window // 2
            if len(session.messages) <= keep_count:
                logger.debug(
                    f"Session {session.key}: No consolidation needed (messages={len(session.messages)}, keep={keep_count})"
                )
                return
            messages_to_process = len(session.messages) - session.last_consolidated
            if messages_to_process <= 0:
                logger.debug(
                    f"Session {session.key}: No new messages to consolidate (last_consolidated={session.last_consolidated}, total={len(session.messages)})"
                )
                return
            old_messages = session.messages[session.last_consolidated : -keep_count]
            if not old_messages:
                return
            logger.info(
                f"Memory consolidation started: {len(session.messages)} total, {len(old_messages)} new to consolidate, {keep_count} keep"
            )

        # 以下逻辑对 archive_all 和普通压缩均适用
        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            lines.append(
                f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}: {m['content']}"
            )
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()
        current_questions = memory.read_questions()

        prompt = f"""你是记忆提取代理（Memory Extraction Agent）。从对话中提取需要长期记住的新事实，返回 JSON。

JSON 包含以下三个键：

1. "history_entry"：2-5 句话的事件摘要，以 [YYYY-MM-DD HH:MM] 开头，保留足够细节便于未来 grep 检索。

2. "new_facts"：本次对话中出现的**新持久化事实**，格式为带分类标注的 bullet 列表。
   规则：
   - 只写现有档案中**没有**的新信息（对照下方档案查重）
   - 只写持久性事实：姓名/设备/账号/偏好/技能/项目经历/游戏数据等
   - 不写一次性操作记录（"帮用户执行了X"、"已完成Y"）
   - 不写对话本身的过程描述
   - 若无新事实，返回空字符串 ""
   - 格式示例：
     - [用户画像] 用户确认正在准备秋招
     - [硬件与环境] 新增显示器：Dell U2723D

3. "answered_question_indices"：从待了解问题列表中，本次对话**已得到解答**的问题序号列表（1-based int）。若无则返回 []。

## 当前用户档案（用于查重，不要重复已有内容）
{current_memory or "（空）"}

## 待了解的问题
{current_questions or "（无）"}

## 待处理对话
{conversation}

只返回合法 JSON，不要 markdown 代码块。"""

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你是记忆提取代理，只返回合法 JSON。",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=[],
                model=self.model,
                max_tokens=1024,
            )
            text = (response.content or "").strip()

            if not text:
                logger.warning(
                    "Memory consolidation: LLM returned empty response, skipping"
                )
                return
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json_repair.loads(text)
            if not isinstance(result, dict):
                logger.warning(
                    f"Memory consolidation: unexpected response type, skipping. Response: {text[:200]}"
                )
                return

            if "history_entry" in result:
                memory.append_history(result["history_entry"])
            # 增量事实写入 PENDING.md，不触碰 MEMORY.md
            # MEMORY.md 由夜间 MemoryOptimizer 统一合并维护
            new_facts = result.get("new_facts", "")
            if new_facts and isinstance(new_facts, str) and new_facts.strip():
                memory.append_pending(new_facts)
                logger.info(
                    f"Memory consolidation: appended {len(new_facts.splitlines())} new facts to PENDING"
                )
            answered = result.get("answered_question_indices", [])
            if answered and isinstance(answered, list):
                indices = [
                    int(i) for i in answered if str(i).isdigit() or isinstance(i, int)
                ]
                if indices:
                    memory.remove_questions_by_indices(indices)
                    logger.info(
                        f"Memory consolidation: removed answered questions {indices}"
                    )

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = len(session.messages) - keep_count
            logger.info(
                f"Memory consolidation done: {len(session.messages)} messages, last_consolidated={session.last_consolidated}"
            )
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender="user",
            chat_id=chat_id,
            content=content,
        )

        response = await self._process(msg, session_key=session_key)
        return response.content if response else ""
