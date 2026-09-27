from __future__ import annotations

from pathlib import Path

from plugins.default_memory.config import (
    load_default_memory_config,
    render_default_memory_config,
    resolve_memory_db_path,
)


def test_default_memory_config_reads_example_defaults() -> None:
    cfg = load_default_memory_config()

    assert cfg.retrieval.top_k_history == 8
    assert cfg.retrieval.thresholds.procedure == 0.66
    assert cfg.retrieval.inject.max_chars == 6000
    # C13 新增节（ADR-1/2/5）：默认值与 dataclass 一致。
    assert cfg.retrieval.sparse.normalization_k == 4.0
    assert cfg.retrieval.sparse.hotness_alpha == 0.2
    assert cfg.retrieval.rrf.k == 60
    assert cfg.retrieval.rrf.keyword_weight == 0.5
    assert cfg.retrieval.rrf.hotness_beta == 0.05
    assert cfg.retrieval.experimental.hyde_enabled is False
    assert cfg.retrieval.experimental.query_rewrite_enabled is False
    assert cfg.retrieval.experimental.reranker_enabled is False


def test_default_memory_config_local_overrides(tmp_path: Path) -> None:
    (tmp_path / "config.local.toml").write_text(
        """
db_path = "custom/memory.db"

[retrieval]
score_threshold = 0.7

[retrieval.thresholds]
event = 0.8

[retrieval.inject]
max_chars = 3000

[retrieval.sparse]
normalization_k = 8.0
hotness_alpha = 0.3

[retrieval.rrf]
k = 100

[retrieval.experimental]
hyde_enabled = true
""",
        encoding="utf-8",
    )

    cfg = load_default_memory_config(plugin_dir=tmp_path)

    assert cfg.db_path == "custom/memory.db"
    assert cfg.retrieval.top_k_history == 8
    assert cfg.retrieval.score_threshold == 0.7
    assert cfg.retrieval.thresholds.event == 0.8
    assert cfg.retrieval.inject.max_chars == 3000
    # 显式覆盖 + 未覆盖项回落默认。
    assert cfg.retrieval.sparse.normalization_k == 8.0
    assert cfg.retrieval.sparse.hotness_alpha == 0.3
    assert cfg.retrieval.rrf.k == 100
    assert cfg.retrieval.rrf.keyword_weight == 0.5
    assert cfg.retrieval.rrf.hotness_beta == 0.05
    assert cfg.retrieval.experimental.hyde_enabled is True
    assert cfg.retrieval.experimental.query_rewrite_enabled is False
    assert cfg.retrieval.experimental.reranker_enabled is False


def test_default_memory_config_render_covers_c13_sections() -> None:
    rendered = render_default_memory_config()

    assert "[retrieval.sparse]" in rendered
    assert "normalization_k = 4.0" in rendered
    assert "hotness_alpha = 0.2" in rendered
    assert "[retrieval.rrf]" in rendered
    assert "k = 60" in rendered
    assert "keyword_weight = 0.5" in rendered
    assert "hotness_beta = 0.05" in rendered
    assert "[retrieval.experimental]" in rendered
    assert "hyde_enabled = false" in rendered
    assert "query_rewrite_enabled = false" in rendered
    assert "reranker_enabled = false" in rendered


def test_default_memory_db_path_resolves_under_workspace(tmp_path: Path) -> None:
    cfg = load_default_memory_config(plugin_dir=tmp_path)

    assert resolve_memory_db_path(workspace=tmp_path, default_config=cfg) == (
        tmp_path / "memory" / "memory2.db"
    )


def test_root_config_example_does_not_expose_default_memory_private_config() -> None:
    text = Path("config.example.toml").read_text(encoding="utf-8")

    assert "[memory.embedding]" in text
    assert "[memory.retrieval]" not in text
    assert "[memory.gate]" not in text
    assert "[memory.hyde]" not in text
    assert "output_dimensionality" not in text
    assert "[memory_v2]" not in text
