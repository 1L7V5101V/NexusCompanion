"""备份契约：backup manifest schema、模板加载与校验器（C12）。

backup manifest 是声明式 JSON 文档（ADR-6），模板 fixture 位于
tests/fixtures/backup_manifest_template.json；后续 change 落地新 canonical
store 时在同一 change 内登记条目并通过 `validate_manifest`（伴随落地协议）。
"""

from core.backup.manifest import (
    MANIFEST_VERSION,
    BackupManifest,
    ManifestEntry,
    ManifestError,
    load_manifest,
    parse_manifest,
    require_valid_manifest,
    validate_manifest,
)

__all__ = [
    "MANIFEST_VERSION",
    "BackupManifest",
    "ManifestEntry",
    "ManifestError",
    "load_manifest",
    "parse_manifest",
    "require_valid_manifest",
    "validate_manifest",
]
