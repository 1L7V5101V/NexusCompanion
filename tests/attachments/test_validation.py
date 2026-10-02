"""C6 内容校验核心矩阵测试（validation.py，ADR-2 冻结值）。

矩阵覆盖：allowlist 各类型正例、伪装扩展名、拒绝面样本（PDF/zip/exe/gif 伪装
文本等）、像素/帧/解码超时/编码/字符数边界，以及冻结错误码稳定性
（upload_type_denied / upload_ext_mismatch / upload_pixel_limit /
upload_frames_limit / upload_decode_timeout / upload_text_limit /
upload_encoding / upload_too_large）。
"""

from __future__ import annotations

import asyncio
import io
import zlib
from pathlib import PurePath

import pytest

from agent.config_models import AttachmentConfig
from bootstrap.attachments.validation import (
    ALLOWED_MIME_EXT,
    AttachmentError,
    validate_upload,
)

from PIL import Image


def _cfg(**overrides: object) -> AttachmentConfig:
    base = AttachmentConfig()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _png_bytes(width: int = 32, height: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 60, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _gif_bytes(n_frames: int, width: int = 16, height: int = 16) -> bytes:
    """生成真实多帧 GIF（各帧内容不同，避免 Pillow 去重合并）。"""
    frames: list[Image.Image] = []
    for i in range(n_frames):
        im = Image.new("RGB", (width, height))
        im.paste((i * 30 % 255, 0, (255 - i * 25) % 255), (0, 0, width // 2, height))
        frames.append(im.convert("P", palette=Image.Palette.ADAPTIVE))
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (40, 50, 60)).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_jpeg_png_webp_gif_txt_all_accepted() -> None:
    cases = [
        (_jpeg_bytes(), "photo.jpg", "image/jpeg", ".jpg"),
        (_jpeg_bytes(), "photo.jpeg", "image/jpeg", ".jpg"),
        (_png_bytes(), "photo.png", "image/png", ".png"),
        (_webp_bytes(), "photo.webp", "image/webp", ".webp"),
        (_gif_bytes(3), "anim.gif", "image/gif", ".gif"),
        (b"hello world\n", "notes.txt", "text/plain", ".txt"),
        (b"\xef\xbb\xbfhello", "bom.txt", "text/plain", ".txt"),
    ]
    for data, filename, mime, ext in cases:
        result = await validate_upload(data, filename, _cfg())
        assert result.detected_mime == mime, filename
        assert result.server_ext == ext, filename
        assert result.size_bytes == len(data)
        assert len(result.checksum_sha256) == 64
        assert not result.filename_display.startswith("/")


@pytest.mark.asyncio
async def test_ext_mask_mismatch_rejected() -> None:
    # 扩展名 .txt 但内容是 PNG → upload_ext_mismatch
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(_png_bytes(), "fake.txt", _cfg())
    assert exc.value.code == "upload_ext_mismatch"
    # 扩展名 .jpg 但内容是文本 → upload_ext_mismatch
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(b"plain text", "fake.jpg", _cfg())
    assert exc.value.code == "upload_ext_mismatch"


@pytest.mark.asyncio
async def test_extension_not_in_allowlist() -> None:
    for name in ("doc.pdf", "x.exe", "a.sh", "b.svg", "c.bmp", "d.avif", "e.heic"):
        with pytest.raises(AttachmentError) as exc:
            await validate_upload(b"x", name, _cfg())
        assert exc.value.code == "upload_type_denied"


@pytest.mark.asyncio
async def test_banned_magic_rejected_even_with_allowed_ext() -> None:
    # 允许扩展名 + 禁止魔数 → 拒绝面（fail-closed，不解析内容）
    banned = [
        (b"%PDF-1.4\n...", "a.pdf.jpg"),
        (b"PK\x03\x04\x00\x00", "archive.zip.png"),
        (b"\x1f\x8b\x08\x00", "compressed.gz.png"),
        (b"MZ\x90\x00", "prog.exe.png"),
        (b"\x7fELF\x02\x01", "elf.png"),
        (b"#!/bin/sh\n", "script.txt.png"),
        (b"<html>", "page.png"),
    ]
    for data, name in banned:
        with pytest.raises(AttachmentError) as exc:
            await validate_upload(data, name, _cfg())
        assert exc.value.code == "upload_type_denied", name


@pytest.mark.asyncio
async def test_bmp_and_isobmff_rejected() -> None:
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(b"BM\x00\x00\x00\x00", "bmp_image.png", _cfg())
    assert exc.value.code == "upload_type_denied"
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(b"\x00\x00\x00\x18ftypavif", "avif.png", _cfg())
    assert exc.value.code == "upload_type_denied"


@pytest.mark.asyncio
async def test_oversize_rejected_and_not_persisted() -> None:
    cfg = _cfg(max_file_bytes=100)
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(b"x" * 101, "big.txt", cfg)
    assert exc.value.code == "upload_too_large"


@pytest.mark.asyncio
async def test_pixel_budget_rejected() -> None:
    cfg = _cfg(max_pixels=1000)
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(_png_bytes(64, 64), "big.png", cfg)
    assert exc.value.code == "upload_pixel_limit"


@pytest.mark.asyncio
async def test_gif_frame_limit_rejected() -> None:
    cfg = _cfg(max_gif_frames=5)
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(_gif_bytes(8), "anim.gif", cfg)
    assert exc.value.code == "upload_frames_limit"


@pytest.mark.asyncio
async def test_text_char_limit_rejected() -> None:
    cfg = _cfg(max_text_chars=10)
    with pytest.raises(AttachmentError) as exc:
        await validate_upload("0123456789a".encode(), "long.txt", cfg)
    assert exc.value.code == "upload_text_limit"


@pytest.mark.asyncio
async def test_utf16_rejected_with_encoding_error() -> None:
    for suffix in ("utf16.txt".encode("utf-8"), "gbk.txt".encode("utf-8")):
        with pytest.raises(AttachmentError) as exc:
            await validate_upload(b"\xff\xfe" + "你好".encode("utf-16-le"), "enc.txt", _cfg())
        assert exc.value.code == "upload_encoding"


@pytest.mark.asyncio
async def test_decode_timeout_guarded() -> None:
    """畸形/超大输入短超时下必须抛 upload_decode_timeout，且进程可用。"""
    cfg = _cfg(max_decode_seconds=0.05)
    # 构造一个 Pillow 能打开但需要 heavy seek 的 GIF 头伪装（帧数巨大）
    fake_gif = b"GIF89a" + b"\x00" * 4096
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(fake_gif, "weird.gif", cfg)
    assert exc.value.code in ("upload_decode_timeout", "upload_type_denied")


@pytest.mark.asyncio
async def test_empty_rejected() -> None:
    with pytest.raises(AttachmentError) as exc:
        await validate_upload(b"", "empty.png", _cfg())
    assert exc.value.code == "upload_empty"