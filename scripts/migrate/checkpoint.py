"""断点续传 checkpoint：JSON 原子写，逐表高水位（D2）。

checkpoint 文件为 ``<run-id>.json``；写采用「临时文件 + rename 原子替换」。
高水位按表记录（主键边界值），恢复时跳过已完成批次。幂等由导入层
``ON CONFLICT DO NOTHING`` 兜底（见 importer），checkpoint 只做跳过优化，
高水位语义允许「多导入一行」也不产生重复。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunCheckpoint:
    """一次运行（run_id）的逐表高水位。"""

    run_id: str
    tables: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def set(self, table: str, high_water: Any) -> None:
        self.tables[table] = high_water

    def get(self, table: str) -> Any | None:
        return self.tables.get(table)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunCheckpoint":
        return cls(
            run_id=str(data.get("run_id", "")),
            tables=dict(data.get("tables", {})),
            updated_at=str(data.get("updated_at", "")),
        )


def load_checkpoint(path: Path) -> RunCheckpoint | None:
    """读 checkpoint；文件缺失或损坏返回 None（启动新运行，幂等兜底）。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return RunCheckpoint.from_json(data)


def save_checkpoint(path: Path, ckpt: RunCheckpoint) -> None:
    """原子写：临时文件 + rename 替换。写失败不破坏已存在 checkpoint。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ckpt.to_json(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
