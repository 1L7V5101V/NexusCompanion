"""backup manifest schema、加载与校验（C12，ADR-6；§5.9.12 冻结语义）。

manifest 为声明式 JSON 文档：`manifest_version` / `created_at` / `entries[]` /
`restore_order[]`，每条 entry 必含 id、kind、path、encryption、checksum、
consistency_point、retention。校验规则（全部负向可测，见 spec）：

1. kind 集合覆盖 canonical store 全集（postgresql / tenant_workspace / config / secrets）；
2. 任何条目路径命中临时目录（POSIX /tmp、Windows 临时目录）→ 拒绝
   （「/tmp 不属于 durable backup 范围」）；
3. legacy_sqlite 条目必须 restore_mode=legacy_only 且 independent（只恢复到
   legacy 模式，不恢复进 Pilot PostgreSQL）；
4. secrets 必须独立条目并单独声明加密；普通业务条目声明包含 secret → 拒绝；
5. restore_order 恰好覆盖全部条目 id；
6. 每条目一致性点与校验声明必填。

结构错误（缺字段/类型错误）在 parse 阶段抛 `ManifestError`；语义问题由
`validate_manifest` 返回 issue 列表（`require_valid_manifest` 便捷抛错）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MANIFEST_VERSION",
    "KIND_POSTGRESQL",
    "KIND_TENANT_WORKSPACE",
    "KIND_CONFIG",
    "KIND_SECRETS",
    "KIND_LEGACY_SQLITE",
    "RESTORE_MODE_PILOT_PRIMARY",
    "RESTORE_MODE_LEGACY_ONLY",
    "REQUIRED_KINDS",
    "BackupManifest",
    "ManifestEntry",
    "ManifestError",
    "parse_manifest",
    "load_manifest",
    "validate_manifest",
    "require_valid_manifest",
]

MANIFEST_VERSION = 1

KIND_POSTGRESQL = "postgresql"
KIND_TENANT_WORKSPACE = "tenant_workspace"
KIND_CONFIG = "config"
KIND_SECRETS = "secrets"
KIND_LEGACY_SQLITE = "legacy_sqlite"

RESTORE_MODE_PILOT_PRIMARY = "pilot_primary"
RESTORE_MODE_LEGACY_ONLY = "legacy_only"

REQUIRED_KINDS: frozenset[str] = frozenset(
    {KIND_POSTGRESQL, KIND_TENANT_WORKSPACE, KIND_CONFIG, KIND_SECRETS}
)

_ENTRY_REQUIRED_KEYS = (
    "id",
    "kind",
    "path",
    "encryption",
    "checksum",
    "consistency_point",
    "retention",
)

# 临时目录排除（§5.9.12）：POSIX /tmp 与 Windows 用户/系统临时目录。
_POSIX_TEMP_PREFIXES = ("/tmp/", "/tmp")
_WINDOWS_TEMP_RE = re.compile(r"(?i)^[a-z]:\\(?:[^\\]+\\)*(?:temp|tmp)(?:\\|$)")


class ManifestError(ValueError):
    """manifest 结构非法（缺字段/类型错误/JSON 解析失败）。"""


@dataclass(frozen=True)
class ManifestEntry:
    """一个备份条目：路径、加密、校验、一致性点与恢复语义声明。"""

    id: str
    kind: str
    path: str
    encryption: str
    checksum: str
    consistency_point: str
    retention: str
    restore_mode: str = RESTORE_MODE_PILOT_PRIMARY
    independent: bool = False
    contains_secrets: bool = False
    notes: str = ""


@dataclass(frozen=True)
class BackupManifest:
    """backup manifest 文档对象。"""

    created_at: str
    entries: tuple[ManifestEntry, ...]
    restore_order: tuple[str, ...]
    manifest_version: int = MANIFEST_VERSION
    notes: str = ""


def parse_manifest(data: Mapping[str, Any]) -> BackupManifest:
    """从已加载的 JSON dict 解析 manifest；结构问题抛 ManifestError。"""
    if not isinstance(data, Mapping):
        raise ManifestError("manifest 顶层必须是对象")
    version = data.get("manifest_version")
    if not isinstance(version, int):
        raise ManifestError("manifest_version 必须是整数")
    created_at = data.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ManifestError("created_at 必须是非空字符串")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestError("entries 必须是非空数组")
    entries: list[ManifestEntry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise ManifestError(f"entries[{index}] 必须是对象")
        for key in _ENTRY_REQUIRED_KEYS:
            if key not in raw or not isinstance(raw[key], str):
                raise ManifestError(f"entries[{index}].{key} 必须是字符串")
        restore_mode = raw.get("restore_mode", RESTORE_MODE_PILOT_PRIMARY)
        if not isinstance(restore_mode, str) or not restore_mode.strip():
            raise ManifestError(f"entries[{index}].restore_mode 必须是非空字符串")
        independent = raw.get("independent", False)
        contains_secrets = raw.get("contains_secrets", False)
        notes = raw.get("notes", "")
        if not isinstance(independent, bool) or not isinstance(contains_secrets, bool):
            raise ManifestError(
                f"entries[{index}].independent/contains_secrets 必须是布尔值"
            )
        if not isinstance(notes, str):
            raise ManifestError(f"entries[{index}].notes 必须是字符串")
        entries.append(
            ManifestEntry(
                id=raw["id"],
                kind=raw["kind"],
                path=raw["path"],
                encryption=raw["encryption"],
                checksum=raw["checksum"],
                consistency_point=raw["consistency_point"],
                retention=raw["retention"],
                restore_mode=restore_mode,
                independent=independent,
                contains_secrets=contains_secrets,
                notes=notes,
            )
        )
    restore_order = data.get("restore_order")
    if not isinstance(restore_order, list) or not all(
        isinstance(item, str) for item in restore_order
    ):
        raise ManifestError("restore_order 必须是字符串数组")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise ManifestError("notes 必须是字符串")
    return BackupManifest(
        created_at=created_at,
        entries=tuple(entries),
        restore_order=tuple(restore_order),
        manifest_version=version,
        notes=notes,
    )


def load_manifest(path: str | Path) -> BackupManifest:
    """从 JSON 文件加载并解析 manifest；解析失败抛 ManifestError。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest 文件读取/解析失败: {path} ({exc})") from exc
    return parse_manifest(data)


def _is_temp_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("/tmp/") or normalized == "/tmp":
        return True
    if "%TEMP%" in path.upper() or "%TMP%" in path.upper():
        return True
    return bool(_WINDOWS_TEMP_RE.match(path.strip()))


def validate_manifest(manifest: BackupManifest) -> list[str]:
    """校验语义规则，返回 issue 列表（空列表 = 通过）。"""
    issues: list[str] = []
    if manifest.manifest_version != MANIFEST_VERSION:
        issues.append(
            f"manifest_version 应为 {MANIFEST_VERSION}，收到 {manifest.manifest_version}"
        )
    ids = [entry.id for entry in manifest.entries]
    if len(ids) != len(set(ids)):
        issues.append("存在重复的条目 id")
    kinds = {entry.kind for entry in manifest.entries}
    missing_kinds = sorted(REQUIRED_KINDS - kinds)
    if missing_kinds:
        issues.append(f"缺少 canonical store 条目 kind: {', '.join(missing_kinds)}")

    for entry in manifest.entries:
        label = f"条目 {entry.id!r}"
        for field_name in (
            "id",
            "kind",
            "path",
            "encryption",
            "checksum",
            "consistency_point",
            "retention",
        ):
            if not getattr(entry, field_name).strip():
                issues.append(f"{label} 缺少必填声明 {field_name}")
        if entry.path.strip() and _is_temp_path(entry.path):
            issues.append(f"{label} 路径命中临时目录（/tmp 不属于 durable backup）")
        if entry.kind == KIND_LEGACY_SQLITE and (
            entry.restore_mode != RESTORE_MODE_LEGACY_ONLY or not entry.independent
        ):
            issues.append(
                f"{label} kind=legacy_sqlite 必须 restore_mode=legacy_only 且 independent=true"
            )
        if entry.contains_secrets and entry.kind != KIND_SECRETS:
            issues.append(
                f"{label} 声明 contains_secrets=true：secret 必须独立为 "
                f"{KIND_SECRETS} 条目，不得混入普通业务备份"
            )
        if entry.kind == KIND_SECRETS and not entry.encryption.strip():
            issues.append(f"{label} 必须单独声明加密方式")

    order_ids = [item for item in manifest.restore_order if item]  # 保持原序去重检查
    seen: set[str] = set()
    for item in order_ids:
        if item in seen:
            issues.append(f"restore_order 重复引用条目 {item!r}")
        seen.add(item)
    unknown = sorted(set(order_ids) - set(ids))
    if unknown:
        issues.append(f"restore_order 引用未登记条目: {', '.join(unknown)}")
    missing_order = sorted(set(ids) - set(order_ids))
    if missing_order:
        issues.append(f"restore_order 未覆盖条目: {', '.join(missing_order)}")
    return issues


def require_valid_manifest(manifest: BackupManifest) -> None:
    """校验不通过时抛 ManifestError（issue 拼接在消息中）。"""
    issues = validate_manifest(manifest)
    if issues:
        raise ManifestError("manifest 校验失败: " + "; ".join(issues))
