from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from agent.prompting import is_context_frame
from agent.tool_hooks import ToolExecutionRequest
from core.common.diagnostic_log import diagnostic_line
from proactive_v2.config import ProactiveConfig
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.gateway import GatewayResult
from plugins.proactive_flow.tools import TOOL_SCHEMAS, ToolDeps, dispatch

logger = logging.getLogger(__name__)


class ProactiveJudge:
    def __init__(
        self,
        *,
        cfg: ProactiveConfig,
        session_key: str,
        llm_fn: Any | None,
        tool_deps: ToolDeps,
        tool_executor: Any,
        record_step_fn: Callable[..., None],
    ) -> None:
        self._cfg = cfg
        self._session_key = session_key
        self._llm_fn = llm_fn
        self._tool_deps = tool_deps
        self._tool_executor = tool_executor
        self._record_step_fn = record_step_fn

    async def evaluate(
        self,
        ctx: AgentTickContext,
        messages: list[dict],
        gw_result: GatewayResult | None,
    ) -> None:
        # 硬编码 Alert 快速路径：有 alert 就跳过 LLM 工具循环
        if ctx.fetched_alerts:
            await self._execute_alert_path(ctx, messages, gw_result)
            return

        if self._llm_fn is None:
            return

        while ctx.steps_taken < self._cfg.agent_tick_max_steps:
            ok = await self._run_tool_step(messages, ctx, loop_tag="loop", tool_choice="auto")
            if not ok:
                break
            if ctx.terminal_action is not None:
                break

        if ctx.terminal_action == "skip" and gw_result is not None and gw_result.content_meta:
            all_content_ids = {m["id"] for m in gw_result.content_meta}
            classified_ids = ctx.interesting_item_ids | ctx.discarded_item_ids
            unclassified_ids = all_content_ids - classified_ids
            if unclassified_ids:
                ctx.terminal_action = None
                ctx.skip_reason = ""
                ctx.skip_note = ""
                titles_hint = "; ".join(
                    f"{m['id']}（{m['title'][:40]}）"
                    for m in gw_result.content_meta
                    if m["id"] in unclassified_ids
                )
                completeness_msg = (
                    f"【系统提示】以下 {len(unclassified_ids)} 个条目尚未完成分类：\n"
                    f"{titles_hint}\n"
                    "请对每条调用 mark_interesting 或 mark_not_interesting，"
                    "全部分类完毕后再调用 message_push + finish_turn(decision=reply)，或 finish_turn(decision=skip, reason=...)。"
                )
                logger.info(
                    "[proactive_v2] judge completeness: %d unclassified, resetting → %s",
                    len(unclassified_ids),
                    sorted(unclassified_ids),
                )
                messages.append({"role": "user", "content": completeness_msg})
                for _ in range(5):
                    if ctx.terminal_action is not None or ctx.steps_taken >= self._cfg.agent_tick_max_steps:
                        break
                    ok = await self._run_tool_step(messages, ctx, loop_tag="complete")
                    if not ok:
                        break

        if ctx.terminal_action is None and ctx.interesting_item_ids and ctx.steps_taken < self._cfg.agent_tick_max_steps:
            ids_str = ", ".join(sorted(ctx.interesting_item_ids))
            reflection = (
                f"【系统提示】你已将以下条目标记为 interesting：{ids_str}。\n"
                "所有条目均已分类完毕。你必须现在调用 message_push 撰写推送，然后调用 finish_turn(decision=reply)；"
                "或直接调用 finish_turn(decision=skip, reason=...)。不允许直接结束。"
            )
            logger.info(
                "[proactive_v2] judge reflection: interesting=%d, injecting prompt",
                len(ctx.interesting_item_ids),
            )
            messages.append({"role": "user", "content": reflection})
            for _ in range(3):
                if ctx.terminal_action is not None or ctx.steps_taken >= self._cfg.agent_tick_max_steps:
                    break
                ok = await self._run_tool_step(messages, ctx, loop_tag="reflect", tool_choice="auto")
                if not ok:
                    break

    async def _execute_alert_path(
        self,
        ctx: AgentTickContext,
        messages: list[dict],
        gw_result: GatewayResult | None,
    ) -> None:
        """Alert 快速路径：有 alert 时走一次 LLM 生成消息，跳过分类工具循环。

        复用已构建好的 messages（含身份/记忆/规则等上下文），
        追加 alert 数据和一条简短指令，调一次 llm_fn 即收尾。
        """
        # 1. user_busy 检查
        if self._tool_deps.recent_chat_fn:
            try:
                recent_raw = await self._tool_deps.recent_chat_fn(n=20)
            except Exception:
                recent_raw = []
            recent_messages: list[dict] = []
            for m in recent_raw or []:
                content = str(m.get("content") or "")
                if is_context_frame(content):
                    continue
                if m.get("role") == "user" or (
                    m.get("role") == "assistant" and not m.get("proactive")
                ):
                    recent_messages.append(m)
            user_busy = False
            last_user_idx: int | None = None
            for i in range(len(recent_messages) - 1, -1, -1):
                if recent_messages[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                last_msg = recent_messages[last_user_idx]
                ts_str = str(last_msg.get("timestamp") or "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        elapsed = (ctx.now_utc - ts).total_seconds()
                        if 0 <= elapsed <= 1800:
                            has_reply_after = any(
                                recent_messages[j].get("role") == "assistant"
                                for j in range(last_user_idx + 1, len(recent_messages))
                            )
                            if has_reply_after:
                                user_busy = True
                    except (ValueError, TypeError):
                        pass
            if user_busy:
                ctx.terminal_action = "skip"
                ctx.skip_reason = "user_busy"
                return

        # 2. 提取 evidence
        cited: list[str] = []
        for alert in ctx.fetched_alerts:
            ack_server = str(alert.get("ack_server") or "?")
            event_id = str(alert.get("event_id") or alert.get("id") or "?")
            cited.append(f"{ack_server}:{event_id}")
        ctx.cited_item_ids = cited

        # 3. 复用已有的 messages，追加 alert 数据和收尾指令
        alert_lines: list[str] = []
        for i, alert in enumerate(ctx.fetched_alerts, 1):
            title = str(alert.get("title") or "").strip()
            content = str(alert.get("content") or alert.get("body") or "").strip()
            if title and content:
                alert_lines.append(f"[{i}] {title}：{content}")
            elif title:
                alert_lines.append(f"[{i}] {title}")
            elif content:
                alert_lines.append(f"[{i}] {content}")
        messages.append({
            "role": "user",
            "content": "【本轮 Alert】\n" + "\n".join(alert_lines),
        })
        messages.append({
            "role": "user",
            "content": (
                f"本轮有 {len(ctx.fetched_alerts)} 条告警，"
                "请基于上面已有的所有上下文和你的身份风格，"
                "将它们整合成一条自然的推送消息发给用户，"
                "然后调用 finish_turn(decision=reply) 结束。"
                "不要调用除 finish_turn 以外的任何工具。"
            ),
        })

        # 4. 一次 LLM 调用生成消息文本（无工具，纯文本生成）
        llm_response = await self._llm_fn(messages, schemas=[])
        final_text = ""
        if llm_response is not None and isinstance(llm_response, dict):
            final_text = str(llm_response.get("content") or "").strip()
        if not final_text:
            logger.warning(
                "[proactive_v2] alert_path: llm_fn returned no content, using fallback"
            )
            titles = [str(a.get("title", "") or "").strip() for a in ctx.fetched_alerts if a.get("title")]
            contents = [str(a.get("content") or a.get("body") or "").strip() for a in ctx.fetched_alerts if a.get("content") or a.get("body")]
            parts = titles or contents or ["有新的通知"]
            final_text = "；".join(parts)
        ctx.final_message = final_text
        ctx.terminal_action = "reply"

    async def _run_tool_step(
        self,
        messages: list[dict],
        ctx: AgentTickContext,
        *,
        loop_tag: str,
        tool_choice: str | dict = "auto",
        schemas: list[dict] | None = None,
    ) -> bool:
        active_schemas = schemas or TOOL_SCHEMAS
        llm_fn = self._llm_fn
        if llm_fn is None:
            return False
        tool_call = await llm_fn(messages, active_schemas, tool_choice)
        if tool_call is None:
            logger.warning(
                "[proactive_v2] %s: llm_fn returned None at step %d, stopping",
                loop_tag,
                ctx.steps_taken,
            )
            return False
        ctx.record_llm_cache(
            cache_prompt_tokens=tool_call.get("_cache_prompt_tokens"),
            cache_hit_tokens=tool_call.get("_cache_hit_tokens"),
        )
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("input", {})
        arg_summary = json.dumps(tool_args, ensure_ascii=False)[:200]
        logger.info(
            diagnostic_line(
                "ProactiveJudge._run_tool_step",
                event="tool_call",
                flow="proactive",
                phase="agent_loop",
                session=self._session_key,
                tick=ctx.tick_id,
                action=str(tool_name or "-"),
                counts=f"step:{ctx.steps_taken}",
            )
        )
        logger.info(
            "[proactive_v2] %s step %d: %s  args=%s",
            loop_tag,
            ctx.steps_taken,
            tool_name,
            arg_summary,
        )
        ctx.steps_taken += 1
        exec_result = await self._tool_executor.execute(
            ToolExecutionRequest(
                call_id=str(tool_call.get("id") or f"call_{ctx.steps_taken}"),
                tool_name=tool_name,
                arguments=tool_args,
                source="proactive",
                session_key=self._session_key,
            ),
            lambda name, args: dispatch(name, args, ctx, self._tool_deps),
        )
        if exec_result.status == "error":
            logger.warning(
                diagnostic_line(
                    "ProactiveJudge._run_tool_step",
                    event="tool_result",
                    flow="proactive",
                    phase="agent_loop",
                    session=self._session_key,
                    tick=ctx.tick_id,
                    action=str(tool_name or "-"),
                    reason="tool_error",
                    counts=f"step:{ctx.steps_taken}",
                    note=str(exec_result.output)[:160],
                )
            )
            logger.warning("[proactive_v2] %s: tool error: %s", loop_tag, exec_result.output)
            result = str(exec_result.output)
            call_id = tool_call.get("id") or f"call_{ctx.steps_taken}"
            self._record_step_fn(
                ctx,
                phase=f"{loop_tag}:error",
                tool_name=tool_name,
                tool_call_id=str(call_id),
                tool_args=tool_args,
                tool_result_text=result,
            )
            self._append_tool_messages(
                messages,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_call_id=call_id,
                result=result,
            )
            return False
        result = str(exec_result.output)
        logger.info(
            diagnostic_line(
                "ProactiveJudge._run_tool_step",
                event="tool_result",
                flow="proactive",
                phase="agent_loop",
                session=self._session_key,
                tick=ctx.tick_id,
                action=str(tool_name or "-"),
                reason="-",
                counts=f"step:{ctx.steps_taken}",
            )
        )
        call_id = tool_call.get("id") or f"call_{ctx.steps_taken}"
        self._record_step_fn(
            ctx,
            phase=loop_tag,
            tool_name=tool_name,
            tool_call_id=str(call_id),
            tool_args=tool_args,
            tool_result_text=result,
        )
        self._append_tool_messages(
            messages,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=call_id,
            result=result,
        )
        return True

    @staticmethod
    def _append_tool_messages(
        messages: list[dict],
        *,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        result: str,
    ) -> None:
        messages.append({
            "role": "assistant",
            "content": f"调用工具 {tool_name}",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(tool_args, ensure_ascii=False),
                },
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })
