"""C6 附件内容校验核心（openspec/changes/c6-attachment/design.md ADR-2）。

接受面 fail-closed：allowlist（图片 jpeg/png/webp/gif + 文本 txt）双一致
（扩展名映射期望 MIME 与服务端 sniff 判定 MIME 同时 in-allowlist 且一致）；
拒绝面（PDF/压缩包/可执行/HTML/SVG/BMP/AVIF/HEIC）一律拒绝且**不解析内容**；
图片资源硬上限（总像素/解码内存/解码超时/GIF 帧数）；文本仅 UTF-8（容 BOM）
且 ≤ 200k 字符。错误码冻结（见 design ADR-7），全部表示为 ``AttachmentError``
（.code 与 HTTP 层映射一一对应）。

sniff 实现（ADR-2）：纯标准库 magic-byte 表（JPEG/PNG/WebP/GIF/PDF/ZIP/GZIP/
TAR/RAR/7z/BMP 头） + Pillow ``Image.open`` 懒加载 format 印证图片（不 decode
全图；尺寸/帧探测设显式上限）；文本用 ``bytes.decode("utf-8")`` 成败判定。
拒绝面不经过任何解压/解码尝试。
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath

from agent.config_models import AttachmentConfig
from core.telemetry.redaction import redact_text

# ── 冻结 allowlist（§10 PROPOSED DEFAULT + C6 ADR-2；本模块是唯一冻结点） ──

ALLOWED_MIME_EXT: dict[str, tuple[str, ...]] = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
    "text/plain": (".txt",),
}
ALLOWED_MIMES = frozenset(ALLOWED_MIME_EXT)
SERVER_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "text/plain": ".txt",
}
_EXT_TO_MIME = {
    ext: mime
    for mime, exts in ALLOWED_MIME_EXT.items()
    for ext in exts
}

# 拒绝面基线（检测判定用，不解析内容；任一命中即 upload_type_denied）
_BANNED_SIGNATURES: dict[bytes, str] = {
    b"%PDF-": "pdf",
    b"PK\x03\x04": "zip",
    b"PK\x05\x06": "zip-empty",
    b"\x1f\x8b": "gzip",
    b"Rar!\x1a\x07": "rar",
    b"7z\xbc\xaf\x27\x1c": "7z",
    b"MZ": "pe-executable",
    b"\x7fELF": "elf-executable",
    b"#!/": "script",
    b"<html": "html",
    b"<HTML": "html",
    b"<!DOCTYPE": "html",
}
_BMP_SIGNATURES = (b"BM",)
# AVIF 无固定魔数（ISOBMFF box 'ftypavif'），HEIC 同族（'ftypheic'/'ftypmif1'/'ftypheif'）
_ISOBMFF_SIGNATURES = (
    b"\x00\x00\x00\x18ftyp",
    b"\x00\x00\x00\x20ftyp",
)


class AttachmentError(ValueError):
    """上传校验失败（fail-closed）。``code`` 为冻结错误码（design ADR-7）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedAttachment:
    """校验通过的附件内容信息（bytes 已随内容传递，不在本结构里再拷贝引用）。"""

    detected_mime: str
    server_ext: str
    size_bytes: int
    checksum_sha256: str
    # 已清洗的展示文件名（仅展示用，绝不进路径）
    filename_display: str


def _expected_mime_for_filename(filename: str) -> str | None:
    """客户端文件名 → 期望 MIME（allowlist 内扩展名；其他返回 None）。"""
    ext = PurePath(filename).suffix.lower()
    return _EXT_TO_MIME.get(ext)


def _sniff_magic(data: bytes) -> tuple[str | None, str | None]:
    """magic-byte sniff → (mime, banned_kind)。

    - banned_kind 命中（PDF/压缩/可执行/HTML 等）→ (None, kind)；
    - BMP/ISOBMFF（AVIF/HEIC）→ (None, "raster-unknown")；
    - 已知 allowlist 魔数 → (mime, None)；否则 (None, None)（未知）。
    """
    if not data:
        return None, None
    for sig, kind in _BANNED_SIGNATURES.items():
        if data.startswith(sig):
            return None, kind
    if data.startswith(_BMP_SIGNATURES) or any(
        data.startswith(sig) for sig in _ISOBMFF_SIGNATURES
    ):
        return None, "raster-unknown"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", None
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", None
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif", None
    return None, None


def _count_gif_frames(data: bytes, max_frames: int) -> int:
    """GIF 帧数：真实帧数（上限截断返回 max_frames+1 表示已超限）。

    返回 max_frames+1 即「超过上限」（上限本身不是成功阈值）；最多 seek 到
    max_frames+1 帧即断，避免恶意长 GIF 拖死探测。
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        n = 0
        while True:
            try:
                img.seek(n)
            except EOFError:
                return n
            n += 1
            if n > max_frames:
                return n


async def _run_decode_bound(
    probe_fn: Callable[[], dict[str, object]],
    config: AttachmentConfig,
) -> dict[str, object]:
    """解码/帧探测在线程池执行并设超时（ADR-2：不阻塞事件循环）。

    返回封装结果；超时抛 AttachmentError(upload_decode_timeout)。
    """

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(probe_fn), timeout=config.max_decode_seconds
        )
    except asyncio.TimeoutError:
        raise AttachmentError("upload_decode_timeout", "图片解码/探测超时") from None
    return dict(result)


async def validate_upload(
    data: bytes | str,
    filename: str,
    config: AttachmentConfig,
) -> ValidatedAttachment:
    """上传内容校验（字节级探测在事件循环外、带超时）。

    失败抛 ``AttachmentError``（错误码冻结）；成功返回 ``ValidatedAttachment``。
    """

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        raise AttachmentError("upload_empty", "上传内容不能为空")
    if len(data) > config.max_file_bytes:
        raise AttachmentError(
            "upload_too_large", f"超过单文件上限 {config.max_file_bytes} 字节"
        )

    expected_mime = _expected_mime_for_filename(filename)
    if expected_mime is None:
        raise AttachmentError(
            "upload_type_denied",
            f"扩展名不在允许范围: {redact_text(PurePath(filename).name)}",
        )

    magic_mime, banned_kind = _sniff_magic(data)
    if banned_kind is not None:
        raise AttachmentError(
            "upload_type_denied", f"禁止类型（{banned_kind}），fail-closed 拒绝"
        )

    is_image_ext = expected_mime.startswith("image/")
    if is_image_ext and magic_mime not in ALLOWED_MIMES:
        # 扩展名声明是图片但魔数不是 allowlist 图片：可能文本伪装或未知二进制。
        # 尝试严格文本判定——文本可直接判不一致（ext_mismatch）；否则 denied。
        try:
            detected_text = _validate_text(data, config)
        except AttachmentError:
            raise AttachmentError(
                "upload_type_denied",
                "扩展名声明图片但内容无对应图片魔数，拒绝",
            ) from None
        if expected_mime != detected_text:
            raise AttachmentError(
                "upload_ext_mismatch",
                f"扩展名映射 {expected_mime} 与内容判定 {detected_text} 不一致",
            )
        detected_mime = detected_text

    if magic_mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        # 图片：Pillow format 印证 + 像素预算 + 解码内存预算 + GIF 帧数
        def _probe() -> dict[str, object]:
            from PIL import Image, UnidentifiedImageError

            try:
                with Image.open(io.BytesIO(data)) as img:
                    fmt = (img.format or "").upper()
                    if fmt not in {"JPEG", "PNG", "WEBP", "GIF"}:
                        raise AttachmentError(
                            "upload_type_denied",
                            f"内容实际格式不在 allowlist：{fmt or '未知'}",
                        )
                    pixels = img.width * img.height
                    over = pixels > config.max_pixels
                    # 解码后内存按 RGBA 4 字节/像素估算（§5.9.15 冻结 64 MiB 上限）
                    over_decode_bytes = pixels * 4 > config.max_decode_bytes
                    frames = 1
                    if fmt == "GIF":
                        frames = _count_gif_frames(data, config.max_gif_frames)
                    return {
                        "fmt": fmt,
                        "over": over,
                        "over_decode_bytes": over_decode_bytes,
                        "frames": frames,
                    }
            except AttachmentError:
                raise
            except Image.DecompressionBombError:
                raise AttachmentError(
                    "upload_pixel_limit",
                    f"总像素触发 Pillow 炸弹护栏（上限 {config.max_pixels}）",
                ) from None
            except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
                # 截断/畸形/非图片：Pillow 的解码异常面收口为拒绝，绝不冒 500。
                raise AttachmentError(
                    "upload_type_denied", "内容无法按 allowlist 图片解码"
                ) from None

        probe = await _run_decode_bound(_probe, config)
        if bool(probe["over"]):
            raise AttachmentError(
                "upload_pixel_limit", f"总像素超过 {config.max_pixels}"
            )
        if bool(probe["over_decode_bytes"]):
            raise AttachmentError(
                "upload_pixel_limit",
                f"解码后内存超过 {config.max_decode_bytes} 字节（按 RGBA 估算）",
            )
        frames = probe["frames"]
        frames_n = int(frames) if isinstance(frames, int) else 1
        if frames_n > config.max_gif_frames:
            raise AttachmentError(
                "upload_frames_limit", f"GIF 帧数超过 {config.max_gif_frames}"
            )
        if probe["fmt"] == "JPEG":
            detected_mime = "image/jpeg"
        elif probe["fmt"] == "PNG":
            detected_mime = "image/png"
        elif probe["fmt"] == "WEBP":
            detected_mime = "image/webp"
        else:
            detected_mime = "image/gif"
    else:
        # 无图片魔数 → 严格 UTF-8 文本判定（容 BOM）
        detected_mime = _validate_text(data, config)

    if expected_mime != detected_mime:
        raise AttachmentError(
            "upload_ext_mismatch",
            f"扩展名映射 {expected_mime} 与内容判定 {detected_mime} 不一致",
        )

    clean_display = PurePath(filename).name
    if len(clean_display) > 255:
        clean_display = clean_display[-255:]
    return ValidatedAttachment(
        detected_mime=detected_mime,
        server_ext=SERVER_EXT_BY_MIME[detected_mime],
        size_bytes=len(data),
        checksum_sha256=_sha256_hex(data),
        filename_display=clean_display,
    )


def _validate_text(data: bytes, config: AttachmentConfig) -> str:
    """文本判定：仅 UTF-8（容 BOM），≤ max_text_chars，拒 UTF-16/GBK 等。"""
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            body = data[3:]
        else:
            body = data
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachmentError(
            "upload_encoding", "仅支持 UTF-8（含 BOM）编码的纯文本，请转码后重传"
        ) from None
    if len(text) > config.max_text_chars:
        raise AttachmentError(
            "upload_text_limit", f"文本超过 {config.max_text_chars} 字符上限"
        )
    return "text/plain"


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


__all__ = [
    "ALLOWED_MIME_EXT",
    "ALLOWED_MIMES",
    "SERVER_EXT_BY_MIME",
    "AttachmentError",
    "ValidatedAttachment",
    "validate_upload",
]