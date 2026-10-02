"""C6 文本上限与编码错误码冻结测试（Task 2.2）。

覆盖默认冻结值：≤200k 字符、仅 UTF-8（容 BOM）、UTF-16/GBK → upload_encoding，
以及 [agent.attachments] 配置节加载（显式覆盖后行为变化）。
"""

from __future__ import annotations

import pytest

from agent.config import load_config
from agent.config_models import AttachmentConfig
from bootstrap.attachments.validation import AttachmentError, validate_upload


@pytest.mark.asyncio
async def test_default_200k_char_boundary() -> None:
    cfg = AttachmentConfig()  # 冻结默认 max_text_chars = 200_000
    ok = await validate_upload("x" * 200_000, "big_ok.txt", cfg)
    assert ok.detected_mime == "text/plain"
    with pytest.raises(AttachmentError) as exc:
        await validate_upload("x" * 200_001, "big_too.txt", cfg)
    assert exc.value.code == "upload_text_limit"


@pytest.mark.asyncio
async def test_utf16_and_gbk_rejected() -> None:
    cfg = AttachmentConfig()
    # UTF-16 LE（带 BOM）
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(
            "\ufeff你好".encode("utf-16-le"), "u16.txt", cfg
        )
    assert exc.value.code == "upload_encoding"
    # GBK 字节（UTF-8 非法序列）
    gbk = "中文内容".encode("gbk")
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(gbk, "gbk.txt", cfg)
    assert exc.value.code == "upload_encoding"


def test_attachment_config_frozen_defaults() -> None:
    cfg = AttachmentConfig()
    assert cfg.max_file_bytes == 20 * 1024 * 1024
    assert cfg.max_pixels == 4096 * 4096
    assert cfg.max_decode_bytes == 64 * 1024 * 1024
    assert cfg.max_decode_seconds == 5.0
    assert cfg.max_gif_frames == 100
    assert cfg.max_text_chars == 200_000
    assert cfg.temp_ttl_hours == 24
    assert cfg.referenced_ttl_days == 30


def test_attachment_config_loads_from_toml(tmp_path: pytest.TempPathFactory) -> None:
    """[agent.attachments] 显式值可覆盖冻结默认，且缺省时保持冻结默认。"""
    toml = tmp_path / "config.toml"
    toml.write_text(
        "\n".join(
            [
                'provider = "openai"',
                'model = "test"',
                "[llm.main]",
                'model = "test"',
                'api_key = "k"',
                "[agent]",
                'system_prompt = "x"',
                "[agent.attachments]",
                "max_file_bytes = 500",
                "max_text_chars = 30",
            ]
        ),
        encoding="utf-8",
    )

    async def _verify() -> None:
        cfg = load_config(toml)
        assert cfg.attachments.max_file_bytes == 500
        assert cfg.attachments.max_text_chars == 30
        # 未配置项保持冻结默认
        assert cfg.attachments.max_pixels == 4096 * 4096
        assert cfg.attachments.referenced_ttl_days == 30
        # 显式覆盖后行为变化（30 字符上限生效）
        with pytest.raises(AttachmentError) as exc:
            await validate_upload("x" * 31, "t.txt", cfg.attachments)
        assert exc.value.code == "upload_text_limit"

    import asyncio

    asyncio.run(_verify())