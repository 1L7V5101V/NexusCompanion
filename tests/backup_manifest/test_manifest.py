"""backup manifest 校验契约测试（C12 ADR-6，task-12 验收第 4 条）。

正/负向：模板可校验通过；缺 kind、临时目录、legacy 非 legacy-only、secrets
混入业务条目、恢复顺序缺口/未知引用均被拒绝。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.backup.manifest import (
    KIND_SECRETS,
    ManifestError,
    load_manifest,
    parse_manifest,
    require_valid_manifest,
    validate_manifest,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "backup_manifest_template.json"
)


def _template() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _entry(data: dict[str, object], entry_id: str) -> dict[str, object]:
    entries = data["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        if entry["id"] == entry_id:
            return entry
    raise AssertionError(f"模板缺少条目 {entry_id}")


def test_template_validates_clean():
    manifest = load_manifest(FIXTURE_PATH)
    assert validate_manifest(manifest) == []
    require_valid_manifest(manifest)  # 不抛


def test_missing_kind_rejected():
    data = _template()
    entries = data["entries"]
    assert isinstance(entries, list)
    data["entries"] = [e for e in entries if e.get("kind") != KIND_SECRETS]
    issues = validate_manifest(parse_manifest(data))
    assert any("secrets" in issue for issue in issues)


def test_missing_required_field_structurally_rejected():
    data = _template()
    entry = _entry(data, "pg-canonical")
    del entry["consistency_point"]
    with pytest.raises(ManifestError):
        parse_manifest(data)


def test_empty_declaration_rejected():
    data = _template()
    entry = _entry(data, "tenant-workspace")
    entry["checksum"] = "  "
    issues = validate_manifest(parse_manifest(data))
    assert any("checksum" in issue for issue in issues)


def test_posix_tmp_path_rejected():
    data = _template()
    entry = _entry(data, "pg-canonical")
    entry["path"] = "/tmp/backup/pg"
    issues = validate_manifest(parse_manifest(data))
    assert any("临时目录" in issue for issue in issues)


def test_windows_temp_path_rejected():
    data = _template()
    entry = _entry(data, "tenant-workspace")
    entry["path"] = r"C:\Users\hp\AppData\Local\Temp\workspace_backup"
    issues = validate_manifest(parse_manifest(data))
    assert any("临时目录" in issue for issue in issues)


def test_legacy_entry_requires_legacy_only_and_independent():
    data = _template()
    entry = _entry(data, "legacy-sqlite")
    entry["restore_mode"] = "pilot_primary"
    issues = validate_manifest(parse_manifest(data))
    assert any("legacy_sqlite" in issue for issue in issues)

    data2 = _template()
    entry2 = _entry(data2, "legacy-sqlite")
    entry2["independent"] = False
    issues2 = validate_manifest(parse_manifest(data2))
    assert any("legacy_sqlite" in issue for issue in issues2)


def test_secrets_must_not_mix_into_business_entry():
    data = _template()
    entry = _entry(data, "config")
    entry["contains_secrets"] = True
    issues = validate_manifest(parse_manifest(data))
    assert any(
        "secrets" in issue.lower() or "secret" in issue.lower() for issue in issues
    )


def test_restore_order_must_cover_all_entries():
    data = _template()
    order = data["restore_order"]
    assert isinstance(order, list)
    data["restore_order"] = [i for i in order if i != "tenant-workspace"]
    issues = validate_manifest(parse_manifest(data))
    assert any("tenant-workspace" in issue for issue in issues)


def test_restore_order_unknown_reference_rejected():
    data = _template()
    order = data["restore_order"]
    assert isinstance(order, list)
    data["restore_order"] = [*order, "ghost-entry"]
    issues = validate_manifest(parse_manifest(data))
    assert any("ghost-entry" in issue for issue in issues)


def test_restore_order_duplicate_reference_rejected():
    data = _template()
    order = data["restore_order"]
    assert isinstance(order, list)
    data["restore_order"] = [*order, order[0]]
    issues = validate_manifest(parse_manifest(data))
    assert any("重复" in issue for issue in issues)


def test_duplicate_entry_ids_rejected():
    data = _template()
    entries = data["entries"]
    assert isinstance(entries, list)
    duplicated = [copy.deepcopy(e) for e in entries[:1]]
    data["entries"] = [*entries, *duplicated]
    issues = validate_manifest(parse_manifest(data))
    assert any("重复" in issue for issue in issues)


def test_wrong_manifest_version_rejected():
    data = _template()
    data["manifest_version"] = 99
    issues = validate_manifest(parse_manifest(data))
    assert any("manifest_version" in issue for issue in issues)


def test_malformed_json_raises_manifest_error(tmp_path: Path):
    bad = tmp_path / "manifest.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(bad)
