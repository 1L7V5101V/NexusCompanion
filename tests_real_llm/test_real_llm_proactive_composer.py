from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.config import load_config
from agent.provider import LLMProvider
from core.net.http import (
    SharedHttpResources,
    clear_default_shared_http_resources,
    configure_default_shared_http_resources,
)
from proactive.composer import Composer
from proactive.event import GenericContentEvent
from proactive.loop_helpers import _format_items, _format_recent


def _patch_real_openai() -> None:
    # 1. pytest 会在 conftest 里注入 openai stub，这里显式切回真实包。
    for name in list(sys.modules):
        if name == "openai" or name.startswith("openai."):
            del sys.modules[name]
    real_openai = importlib.import_module("openai")
    import agent.provider as provider_mod

    provider_mod.AsyncOpenAI = real_openai.AsyncOpenAI


def _item(title: str, content: str, url: str, minutes_ago: int) -> GenericContentEvent:
    return GenericContentEvent(
        event_id=title,
        source_name="PC Gamer UK - Games",
        source_type="rss",
        title=title,
        content=content,
        url=url,
        published_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_real_llm_composer_aggregates_updates_with_per_item_links():
    cfg_path = Path("/mnt/data/coding/akasic-agent/config.json")
    _patch_real_openai()
    cfg = load_config(str(cfg_path))
    provider = LLMProvider(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        system_prompt=cfg.system_prompt,
        extra_body=cfg.extra_body,
        request_timeout_s=60,
        max_retries=0,
    )
    resources = SharedHttpResources()
    configure_default_shared_http_resources(resources)
    try:
        composer = Composer(
            provider=provider,
            model=cfg.model,
            max_tokens=700,
            format_items=_format_items,
            format_recent=_format_recent,
        )
        items = [
            _item(
                "Banquet for Fools release date confirmed",
                "PC Gamer 报道 Banquet for Fools 的发售窗口已经确认，开发者同时放出一批新截图。",
                "https://www.pcgamer.com/banquet-release",
                8,
            ),
            _item(
                "Banquet for Fools demo is out now",
                "试玩版已经上线，首章内容可直接体验，PC Gamer 还提到战斗系统比预期更复杂。",
                "https://www.pcgamer.com/banquet-demo",
                5,
            ),
            _item(
                "Banquet for Fools gets combat deep dive",
                "最新深度稿细讲了构筑、Boss 节奏和资源管理，整体风格不像一次性快讯，更像连续放料。",
                "https://www.pcgamer.com/banquet-combat",
                2,
            ),
        ]

        message = await composer.compose_for_judge(
            items=items,
            recent=[],
            preference_block="",
        )

        assert message.strip()
        assert len(message) <= 400
        assert "Banquet for Fools" in message
        assert "https://www.pcgamer.com/banquet-release" in message
        assert "https://www.pcgamer.com/banquet-demo" in message
    finally:
        clear_default_shared_http_resources(resources)
        await resources.aclose()
