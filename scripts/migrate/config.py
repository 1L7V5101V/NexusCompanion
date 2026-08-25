"""迁移工具配置：路径、批量大小、PG 连接、evidence 目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 生产默认值：本地 staging PG（docker/debug/docker-compose.yml 同款）。
DEFAULT_PG_URL = "postgresql://nexus:nexus_dev@localhost:5433/nexus"
# 迁移证据根目录（与 phase1-storage evidence 模式一致）。
DEFAULT_EVIDENCE_DIR = Path("openspec/evidence/phase1b")
# 默认 batch 大小（COPY 分片 + checkpoint 粒度）。
DEFAULT_BATCH_SIZE = 5000

# 源身份常量：memory 系列表无通道维度，统一归到该 identity。
MEMORY_SOURCE_IDENTITY = "memory"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_pg_url(override: str | None = None) -> str:
    """优先显式 override，其次环境变量 NEXUS_TEST_PG_URL，最后本地默认。"""
    if override:
        return override
    return os.environ.get("NEXUS_TEST_PG_URL", DEFAULT_PG_URL)


@dataclass(frozen=True)
class MigrationConfig:
    """一次迁移运行的全部配置。字段不变；运行期可变状态放 checkpoint/state。"""

    workspace: Path
    pg_url: str = DEFAULT_PG_URL
    mapping_path: Path | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    run_id: str = field(default_factory=utc_now_iso)
    checkpoint_dir: Path = DEFAULT_EVIDENCE_DIR / "checkpoints"
    results_dir: Path = DEFAULT_EVIDENCE_DIR / "results"
    state_path: Path = DEFAULT_EVIDENCE_DIR / "cutover_state.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.checkpoint_dir / f"{self.run_id}.json"

    def with_run_id(self, run_id: str) -> "MigrationConfig":
        return MigrationConfig(
            workspace=self.workspace,
            pg_url=self.pg_url,
            mapping_path=self.mapping_path,
            batch_size=self.batch_size,
            run_id=run_id,
            checkpoint_dir=self.checkpoint_dir,
            results_dir=self.results_dir,
            state_path=self.state_path,
        )

    def ensure_dirs(self) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_args(
        cls,
        *,
        workspace: str | Path,
        pg_url: str | None = None,
        mapping: str | Path | None = None,
        batch_size: int | None = None,
        run_id: str | None = None,
        checkpoint_dir: str | Path | None = None,
        results_dir: str | Path | None = None,
        state_path: str | Path | None = None,
        base_dir: Path = Path.cwd(),
    ) -> "MigrationConfig":
        """从 CLI 参数构造；相对路径以 base_dir（默认 cwd）解析。"""
        def _abs(p: str | Path | None, default: Path) -> Path:
            if p is None:
                return (base_dir / default).resolve()
            return (Path(p) if Path(p).is_absolute() else base_dir / p).resolve()

        return cls(
            workspace=Path(workspace).resolve() if not Path(workspace).is_absolute()
            else Path(workspace),
            pg_url=resolve_pg_url(pg_url),
            mapping_path=_abs(mapping, Path("mapping.json")) if mapping is not None else None,
            batch_size=batch_size or DEFAULT_BATCH_SIZE,
            run_id=run_id or utc_now_iso(),
            checkpoint_dir=_abs(checkpoint_dir, DEFAULT_EVIDENCE_DIR / "checkpoints"),
            results_dir=_abs(results_dir, DEFAULT_EVIDENCE_DIR / "results"),
            state_path=_abs(state_path, DEFAULT_EVIDENCE_DIR / "cutover_state.json"),
        )

    def to_meta(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "pg_url": self.pg_url,
            "batch_size": self.batch_size,
            "run_id": self.run_id,
            "checkpoint_path": str(self.checkpoint_path),
        }
