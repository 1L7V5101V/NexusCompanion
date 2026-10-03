"""retention 配置冻结默认与加载期校验（tasks 2.1/2.2/2.3）。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent.config import load_config
from agent.config_models import RetentionConfig
from core.telemetry.retention import (
    AUDIT_DEFAULT_DAYS,
    DEBUG_CONTENT_DEFAULT_DAYS,
    OPERATIONAL_DEFAULT_DAYS,
    RetentionPolicy,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_retention_config_frozen_defaults() -> None:
    cfg = RetentionConfig()
    # 三档天数 = core.telemetry.retention 冻结常量（单一来源，不重复写数字）。
    assert cfg.operational_days == OPERATIONAL_DEFAULT_DAYS
    assert cfg.audit_days == AUDIT_DEFAULT_DAYS
    assert cfg.debug_content_days == DEBUG_CONTENT_DEFAULT_DAYS
    assert (cfg.operational_days, cfg.audit_days, cfg.debug_content_days) == (30, 180, 7)
    # 与 RetentionPolicy() 默认一致（同一来源断言）。
    policy = RetentionPolicy()
    assert cfg.operational_days == policy.operational_days
    assert cfg.audit_days == policy.audit_days
    assert cfg.debug_content_days == policy.debug_content_days
    # 周期 / 总开关 / 分批（tasks 2.1 冻结值）。
    assert cfg.enabled is True
    assert cfg.interval_s == 86400
    assert cfg.batch_size == 500
    assert cfg.max_batches == 20
    # owner 暂定默认（task 0.1：design 候选值，纯 config 可改）。
    assert cfg.purge_grace_s == 30 * 86400
    assert cfg.replay_keep_last_frames == 20
    assert cfg.replay_max_age_days == 30
    # 文件腿内置 root 集为空（ADR-1）。
    assert cfg.file_roots == {}


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                'model = "test"',
                "[llm.main]",
                'model = "test"',
                'api_key = "k"',
                "[agent]",
                'system_prompt = "x"',
                body,
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_loader_accepts_unset_and_zero_interval(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    cfg = load_config(path)
    assert cfg.retention.enabled is True
    assert cfg.retention.interval_s == 86400

    path = _write(
        tmp_path,
        "[agent.retention]\nenabled = false\ninterval_s = 0\n",
    )
    cfg = load_config(path)
    assert cfg.retention.enabled is False
    assert cfg.retention.interval_s == 0  # 0 = 不启用周期任务


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("operational_days", "0"),
        ("operational_days", "-5"),
        ("audit_days", "0"),
        ("debug_content_days", "-1"),
        ("batch_size", "0"),
        ("max_batches", "-2"),
        ("replay_keep_last_frames", "0"),
        ("replay_max_age_days", "0"),
        ("purge_grace_s", "-30"),
    ],
)
def test_loader_rejects_non_positive(tmp_path: Path, key: str, value: str) -> None:
    path = _write(tmp_path, f"[agent.retention]\n{key} = {value}\n")
    with pytest.raises(ValueError, match=f"agent.retention.{key}"):
        load_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("operational_days", "'thirty'"),
        ("interval_s", "true"),
        ("replay_max_age_days", "1.5"),
    ],
)
def test_loader_rejects_non_numeric(tmp_path: Path, key: str, value: str) -> None:
    path = _write(tmp_path, f"[agent.retention]\n{key} = {value}\n")
    with pytest.raises(ValueError, match=f"agent.retention.{key}"):
        load_config(path)


def test_loader_rejects_unknown_file_root_category(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[agent.retention]\nfile_roots = { typo_category = ["/tmp/x"] }\n',
    )
    with pytest.raises(ValueError, match="file_roots 类别"):
        load_config(path)


def test_loader_rejects_bad_file_roots_shape(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[agent.retention]\nfile_roots = { operational = [""] }\n',
    )
    with pytest.raises(ValueError, match="file_roots.operational"):
        load_config(path)


def test_loader_accepts_valid_file_roots(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[agent.retention]\n'
        'file_roots = { operational = ["/data/shards"], audit = ["/data/audit"] }\n',
    )
    cfg = load_config(path)
    assert cfg.retention.file_roots == {
        "operational": ["/data/shards"],
        "audit": ["/data/audit"],
    }


def test_config_example_toml_parses_with_retention_block() -> None:
    raw = tomllib.loads((REPO_ROOT / "config.example.toml").read_text(encoding="utf-8"))
    # example 文件整段注释：解析通过即断言无语法破坏。
    assert isinstance(raw, dict)
    cfg = load_config(REPO_ROOT / "config.example.toml")
    assert cfg.retention.enabled is True
    assert cfg.retention.operational_days == 30
