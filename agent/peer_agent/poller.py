"""
PeerAgentPoller：后台 asyncio 任务，轮询所有 pending A2A 任务。

完成时：
  1. 向 MessageBus 注入系统触发消息，让 AgentLoop 用 read_file 读报告并总结
  2. 通知 ProcessManager 销毁子进程
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bus.events import InboundMessage
from bus.queue import MessageBus
from core.net.http import HttpRequester, RequestBudget

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 10
_TASK_TIMEOUT_S = 3600      # 60 分钟硬超时（DeepResearch 复杂任务可能需要 30min+）


@dataclass
class _PendingTask:
    task_id: str
    agent_name: str
    agent_url: str
    channel: str
    chat_id: str
    goal: str
    submitted_at: float = field(default_factory=time.monotonic)


class PeerAgentPoller:
    """后台轮询所有 pending A2A 任务，完成后注入 MessageBus 触发新一轮 AgentLoop。"""

    def __init__(
        self,
        bus: MessageBus,
        process_manager,          # PeerProcessManager，避免循环导入
        requester: HttpRequester,
    ) -> None:
        self._bus = bus
        self._pm = process_manager
        self._requester = requester
        self._pending: dict[str, _PendingTask] = {}
        self._task: asyncio.Task | None = None

    def register(
        self,
        *,
        task_id: str,
        agent_name: str,
        agent_url: str,
        channel: str,
        chat_id: str,
        goal: str,
    ) -> None:
        self._pending[task_id] = _PendingTask(
            task_id=task_id,
            agent_name=agent_name,
            agent_url=agent_url,
            channel=channel,
            chat_id=chat_id,
            goal=goal,
        )
        logger.info("[Poller] 注册任务 task_id=%s agent=%s", task_id, agent_name)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="peer_agent_poller")
        logger.info("[Poller] 后台轮询已启动")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Poller] 已停止")

    # ── 内部 ──────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            for task_id, meta in list(self._pending.items()):
                try:
                    await self._check(task_id, meta)
                except Exception as e:
                    logger.warning("[Poller] 检查任务 %s 出错: %s", task_id, e)

    async def _check(self, task_id: str, meta: _PendingTask) -> None:
        # 硬超时
        if time.monotonic() - meta.submitted_at > _TASK_TIMEOUT_S:
            logger.warning("[Poller] 任务 %s 超时（60分钟）", task_id)
            del self._pending[task_id]
            await self._inject_failure(meta, "调研超时（超过60分钟）")
            await self._pm.terminate(meta.agent_name)
            return

        state, artifacts, status_text = await self._get_task_status(meta.agent_url, task_id)

        if state == "completed":
            logger.info("[Poller] 任务 %s 完成，artifacts: %s", task_id, list(artifacts.keys()))
            del self._pending[task_id]
            await self._inject_completion(meta, artifacts)
            await self._pm.terminate(meta.agent_name)

        elif state == "failed":
            logger.warning("[Poller] 任务 %s 失败 原因: %s", task_id, status_text or "(无消息)")
            del self._pending[task_id]
            await self._inject_failure(meta, f"调研任务执行失败：{status_text}" if status_text else "调研任务执行失败")
            await self._pm.terminate(meta.agent_name)

        # 其他状态（submitted/working）静默等待

    async def _get_task_status(
        self, agent_url: str, task_id: str
    ) -> tuple[str, dict[str, str], str]:
        """向 A2A 服务器查询任务状态，返回 (state, artifacts, status_text)。

        artifacts: {name -> text_value}，提取每个 artifact 第一个文本 part。
        """
        payload = {
            "jsonrpc": "2.0",
            "id": "poll-1",
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        r = await self._requester.post(
            agent_url,
            json=payload,
            budget=RequestBudget(total_timeout_s=8.0),
        )
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            raise RuntimeError(f"tasks/get 错误: {data['error']}")

        result = data.get("result", {})
        status = result.get("status", {})
        state = status.get("state", "unknown")

        # 提取状态消息文本，用于失败诊断
        status_parts = status.get("message", {}).get("parts", [])
        status_text = " | ".join(
            p.get("text", "") for p in status_parts if isinstance(p, dict) and p.get("text")
        )
        if status_text:
            logger.debug("[Poller] 任务 %s 状态=%s 消息: %s", task_id, state, status_text)

        # 收集所有 artifacts：{name -> 第一个 text part}
        artifacts: dict[str, str] = {}
        for artifact in result.get("artifacts", []):
            name = artifact.get("name", "")
            if not name:
                continue
            for p in artifact.get("parts", []):
                # A2A SDK 有两种序列化：{text: ...} 或 {root: {text: ...}}
                text = p.get("text") or (
                    p.get("root", {}).get("text") if isinstance(p.get("root"), dict) else None
                )
                if text:
                    artifacts[name] = text
                    break

        return state, artifacts, status_text

    async def _inject_completion(self, meta: _PendingTask, artifacts: dict[str, str]) -> None:
        """向 MessageBus 注入系统消息，触发 AgentLoop 新一轮处理结果并回复用户。"""
        artifact_lines = "\n".join(
            f"  - {name}: {path}" for name, path in artifacts.items()
        ) or "  （无产出文件）"
        text = (
            f"[系统通知] 后台任务已完成。\n"
            f"执行的任务：{meta.goal}\n"
            f"执行者：{meta.agent_name}\n"
            f"产出文件：\n{artifact_lines}\n\n"
            f"请根据产出内容向用户汇报结果。"
        )
        await self._bus.publish_inbound(
            InboundMessage(
                channel=meta.channel,
                sender="system",
                chat_id=meta.chat_id,
                content=text,
                metadata={"system_injected": True, "task_id": meta.task_id},
            )
        )

    async def _inject_failure(self, meta: _PendingTask, reason: str) -> None:
        text = (
            f"[系统通知] 后台任务未能完成：{reason}。\n"
            f"执行的任务：{meta.goal}\n"
            f"执行者：{meta.agent_name}\n"
            f"请告知用户，并建议他们稍后重试。"
        )
        await self._bus.publish_inbound(
            InboundMessage(
                channel=meta.channel,
                sender="system",
                chat_id=meta.chat_id,
                content=text,
                metadata={"system_injected": True, "task_id": meta.task_id},
            )
        )
