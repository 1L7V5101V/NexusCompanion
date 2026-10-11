"""c18 task 2.2：画像层的运行时路径不得指向共享 workspace 记忆目录。

落实 spec「运行时路径无共享目录读写」。本测试在接线完成前预期为红——
`bootstrap/memory.py` 等仍在装配进程级全局 markdown store。

扫描只禁「全局装配」形态的符号，不禁 read_long_term 这类改名后仍然合法的
调用，避免立出一条以后要开例外的宽规则。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

TARGET_FILES = (
    "core/memory/runtime.py",
    "bootstrap/memory.py",
    "bootstrap/tools.py",
    "bootstrap/app.py",
    "bootstrap/proactive.py",
    "agent/core/prompt_block.py",
    "memory2/store.py",
)
TARGET_DIRS = ("plugins/drift_flow",)

FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"build_markdown_memory_runtime\s*\(", "装配全局 markdown 记忆 runtime"),
    (
        r"\.markdown\.(?:store|maintenance)\b",
        "取用进程级 markdown store/maintenance 单例",
    ),
    (r"\bMemoryStore\s*\(", "直接构造文件版 MemoryStore"),
    (
        r"ctx\.memory\.read_self\s*\(",
        "自我认知仍从画像 store 读取（ADR-9 要求只读 persona 快照）",
    ),
)
# 注：不扫 `workspace / "memory"` 这类目录拼接字面量。`bootstrap/tools.py:699`
# 把 `workspace/memory/memory2.db` 作为路径实参传给 create_storage_runtime，而
# infra/storage/runtime.py:67-78 在有 postgres_url 时把 _memory_sqlite 置为
# None——该实参在多租户下不参与构造，不是画像层读写。目录归属由 2.3 的行为测试
# 与上面的构造符号共同覆盖。


def _sources() -> list[Path]:
    files = [REPO / rel for rel in TARGET_FILES]
    for rel_dir in TARGET_DIRS:
        files += sorted((REPO / rel_dir).glob("*.py"))
    return files


def test_scan_targets_exist() -> None:
    """改名或删文件要让本测试失去覆盖时先报警，而不是静默通过。"""
    missing = [
        str(path.relative_to(REPO)) for path in _sources() if not path.exists()
    ]
    assert not missing, f"扫描目标不存在，测试已失去覆盖：{missing}"


def test_runtime_paths_do_not_read_or_write_shared_memory_dir() -> None:
    hits: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for pattern, why in FORBIDDEN:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                hits.append(f"{path.relative_to(REPO)}:{line} — {why}")
    assert not hits, (
        "画像层运行时路径仍指向共享 markdown 记忆目录：\n" + "\n".join(sorted(hits))
    )
