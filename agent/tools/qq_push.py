"""
QQ 主动推送工具

Agent 可调用此工具向 QQ 用户发送消息，无需等待对方先发消息（需已有历史会话）。
"""
import asyncio
import logging
from typing import Any

from agent.tools.base import Tool

logger = logging.getLogger(__name__)


class QQPushTool(Tool):
    """向指定 QQ 用户主动发送消息"""

    name = "qq_push"
    description = (
        "向 QQ 用户主动发送消息。"
        "用户必须曾经给 bot 发过消息（bot 才能知道其 chat_id）。"
        "通过 QQ 号指定目标用户。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "qq": {
                "type": "string",
                "description": "目标用户的 QQ 号，例如 123456789",
            },
            "message": {
                "type": "string",
                "description": "要发送的消息内容",
            },
        },
        "required": ["qq", "message"],
    }

    def __init__(self, channel) -> None:
        """
        channel: QQChannel 实例（共享 api 和 user_map 引用）
        """
        self._channel = channel

    async def execute(self, **kwargs: Any) -> str:
        qq: str = kwargs["qq"].strip()
        message: str = kwargs["message"]

        if qq not in self._channel.user_map:
            return (
                f"未找到 QQ 用户 {qq} 的会话记录。"
                f"该用户需要先给 bot 发一条消息，bot 才能主动联系他。"
                f"当前已知用户：{list(self._channel.user_map.keys()) or '（无）'}"
            )

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._channel._api.send_private_text_sync,
                int(qq),
                message,
            )
            logger.info(f"[qq_push] 已推送消息给 QQ {qq}")
            return f"消息已成功发送给 QQ 用户 {qq}"
        except Exception as e:
            logger.error(f"[qq_push] 发送失败 QQ {qq}: {e}")
            return f"发送失败：{e}"
