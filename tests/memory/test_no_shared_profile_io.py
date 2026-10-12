"""c18 task 2.2 / 2.4：画像层的运行时路径不得指向共享 workspace 记忆目录，
且组装旁路不得自己读画像存储。

落实 spec「运行时路径无共享目录读写」与「画像段渲染只有一份实现」。

task 6.10 之后第一条规则转绿：装配根不再产出文件 store，画像层唯一的入口是按租户解析
出来的存储。第二条规则仍有一处**已登记**的例外（唤醒插件，见 `WAKE_BASELINE`），它不能
在本 change 里消掉，原因写在下面。

两套规则的作用域是分开的：

- `FORBIDDEN` 全局适用，只禁「全局装配」形态的符号——这类符号一旦被引用就说明持有
  的是进程级单例，与租户无关，不存在合法用法，所以不需要给例外留口子。
- `FORBIDDEN_IN_ASSEMBLY_PATHS` 只作用于 `BYPASS_DIRS`。它禁的是插件侧直接读画像，
  依据是 ADR-15：画像段必须经共用的 `render_sections` 渲染，旁路只消费渲染结果。
  同名的 `read_long_term` 在 `core/memory/runtime.py` 的按租户解析入口里仍然合法，
  所以这条规则必须按目录限定，不能升成全局规则——那会让 4.1 要建的解析入口本身违规。
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
# task 2.4：原范围只有 plugins/drift_flow，另两条旁路不在内——而主动推送与唤醒恰恰是
# 同步方法里调 async 存储、最容易把协程对象塞进提示的两条（spec「同步上下文不产生未
# await 的协程」）。default_proactive 也列入，它自己搓消息列表（runtime.py:557-576）。
BYPASS_DIRS = (
    "plugins/proactive_flow",
    "plugins/drift_flow",
    "plugins/wake_proactive",
    "plugins/default_proactive",
)

FORBIDDEN: tuple[tuple[str, str], ...] = (
    (
        r"\.markdown\.store\b",
        "取用进程级 markdown store 单例（task 6.10 已删掉这个属性）",
    ),
    (r"\bMemoryStore\s*\(", "直接构造文件版 MemoryStore"),
    (
        r"ctx\.memory\.read_self\s*\(",
        "自我认知仍从画像 store 读取（ADR-9 要求只读 persona 快照）",
    ),
)

# 用裸标识符而不是 `read_long_term(` ——plugins/wake_proactive/runtime.py:1025 的写法是
# `getattr(self._scope.memory, "read_long_term", None)`，带调用的模式抓不到第二种形态。
# 在旁路目录里宁可过检：这四个目录下出现任何提到画像读取方法的名字，都说明它没有走
# 共用渲染器。
FORBIDDEN_IN_ASSEMBLY_PATHS: tuple[tuple[str, str], ...] = (
    (
        r"\b(?:read_long_term|read_recent_context|read_self)\b",
        "旁路自己读画像存储，未走共用的 render_sections（ADR-15）",
    ),
)
# 注：不扫 `workspace / "memory"` 这类目录拼接字面量。`bootstrap/tools.py:699`
# 把 `workspace/memory/memory2.db` 作为路径实参传给 create_storage_runtime，而
# infra/storage/runtime.py:67-78 在有 postgres_url 时把 _memory_sqlite 置为
# None——该实参在多租户下不参与构造，不是画像层读写。目录归属由 2.3 的行为测试
# 与上面的构造符号共同覆盖。


# 例外登记：唤醒插件那一处读点。`plugins/wake_proactive/runtime.py` 今天不可导入
# （`session/embedding_store` 缺失，见 tasks 4.7 那条注记），改动已移出本 change，所以
# 这条命中在这里是**已知且预期**的。判据因此是「不得出现登记之外的命中」，而不是
# 「必须为零」——后者是一条永远绿不了的规则，既不能当闸门也不该删掉看守。
WAKE_BASELINE: frozenset[str] = frozenset(
    {
        "plugins/wake_proactive/runtime.py — 旁路自己读画像存储，"
        "未走共用的 render_sections（ADR-15）"
    }
)


def _hit_files(hits: list[str]) -> set[str]:
    """把 `path:line — 规则` 收成 `path — 规则`：行号会漂，登记不能按行号锁。"""
    out: set[str] = set()
    for hit in hits:
        path, _, reason = hit.partition(" — ")
        file_only, _, _ = path.rpartition(":")
        # Windows 上扫描结果用反斜杠，登记用正斜杠：比对前统一
        out.add(f"{file_only.replace(chr(92), '/')} — {reason}")
    return out


def _global_sources() -> list[Path]:
    return [REPO / rel for rel in TARGET_FILES]


def _bypass_sources() -> list[Path]:
    files: list[Path] = []
    for rel_dir in BYPASS_DIRS:
        files += sorted((REPO / rel_dir).glob("*.py"))
    return files


def test_scan_targets_exist() -> None:
    """改名或删文件要让本测试失去覆盖时先报警，而不是静默通过。"""
    missing = [
        str(path.relative_to(REPO))
        for path in _global_sources()
        if not path.exists()
    ]
    assert not missing, f"扫描目标不存在，测试已失去覆盖：{missing}"
    empty_dirs = [rel for rel in BYPASS_DIRS if not (REPO / rel).is_dir()]
    assert not empty_dirs, f"旁路目录不存在，测试已失去覆盖：{empty_dirs}"
    assert _bypass_sources(), "BYPASS_DIRS 下没有 .py 文件，旁路规则已成空转"


def _scan(paths: list[Path], rules: tuple[tuple[str, str], ...]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern, why in rules:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                hits.append(f"{path.relative_to(REPO)}:{line} — {why}")
    return hits


def test_runtime_paths_do_not_read_or_write_shared_memory_dir() -> None:
    hits = _scan(_global_sources() + _bypass_sources(), FORBIDDEN)
    assert not hits, (
        "画像层运行时路径仍指向共享 markdown 记忆目录：\n" + "\n".join(sorted(hits))
    )


def test_assembly_paths_do_not_read_profile_store_directly() -> None:
    """task 2.4 / spec「画像段渲染只有一份实现」：旁路只消费渲染结果。

    已登记的唤醒例外允许存在；登记之外新出现的读取点是本测试要拦的东西。行号会漂，
    所以登记按「文件 + 规则」比对，不按行号锁。
    """
    hits = _scan(_bypass_sources(), FORBIDDEN_IN_ASSEMBLY_PATHS)
    fresh = _hit_files(hits) - WAKE_BASELINE
    assert not fresh, (
        "组装旁路新增了自己的画像读取，画像段因此有两份实现：\n" + "\n".join(sorted(fresh))
    )
