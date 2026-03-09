from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from feeds.base import FeedSubscription
from proactive.source_scorer import (
    SourceScorer,
    _parse_scores_json,
    _parse_single_score,
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _sub(source_id: str, name: str) -> FeedSubscription:
    return FeedSubscription(id=source_id, type="rss", name=name)


def test_parse_scores_json_supports_fenced_payload():
    subs = [_sub("s1", "Alpha"), _sub("s2", "Beta")]

    result = _parse_scores_json(
        '```json\n{"scores":{"s1":8.5,"s2":3}}\n```',
        subs,
    )

    assert result == {"s1": 8.5, "s2": 3.0}


def test_parse_scores_json_defaults_missing_source():
    subs = [_sub("s1", "Alpha"), _sub("s2", "Beta")]

    result = _parse_scores_json('{"scores":{"s1":9}}', subs)

    assert result == {"s1": 9.0, "s2": 5.0}


def test_parse_single_score_falls_back_to_plain_number():
    assert _parse_single_score("建议分数是 7.5") == 7.5


@pytest.mark.asyncio
async def test_score_all_sources_uses_shared_json_request_path(tmp_path: Path):
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=_Resp('```json\n{"scores":{"s1":8,"s2":2}}\n```')
    )
    scorer = SourceScorer(provider, "test-model", tmp_path / "source_scores.json")

    result = await scorer._score_all_sources(
        [_sub("s1", "Alpha"), _sub("s2", "Beta")],
        "用户偏好：单机游戏。",
    )

    assert result == {"s1": 8.0, "s2": 2.0}
    kwargs = provider.chat.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["tools"] == []
    assert kwargs["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_score_single_source_uses_shared_json_request_path(tmp_path: Path):
    provider = AsyncMock()
    provider.chat = AsyncMock(return_value=_Resp('{"score": 6.5}'))
    scorer = SourceScorer(provider, "test-model", tmp_path / "source_scores.json")

    result = await scorer._score_single_source(
        _sub("s3", "Gamma"),
        {"s1": 8.0},
        "用户偏好：策略游戏。",
    )

    assert result == 6.5
    kwargs = provider.chat.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["tools"] == []
    assert kwargs["max_tokens"] == 64
