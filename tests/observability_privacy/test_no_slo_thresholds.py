"""静态负向测试：观测契约实现不得包含 SLO/容量红线（ADR-8，§10 DEFERRED BY EVIDENCE）。

扫描 core/telemetry/ 与 core/backup/ 全部模块的 AST：任何名为
slo/p95/p99/rpo/rto 语义的常量赋值都视为红线提前进入代码。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[2] / "core"
SCAN_DIRS = ("telemetry", "backup")
SLO_NAME_RE = re.compile(r"(?i)^(_)?(slo|p95|p99|rpo|rto)(_|\d|$)")


def _assignment_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
                elif isinstance(target, ast.Tuple):
                    names.extend(
                        elt.id for elt in target.elts if isinstance(elt, ast.Name)
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_no_slo_threshold_constants_in_observability_modules():
    offenders: dict[str, list[str]] = {}
    for sub in SCAN_DIRS:
        for path in sorted((CORE_DIR / sub).glob("*.py")):
            hits = [n for n in _assignment_names(path) if SLO_NAME_RE.match(n)]
            if hits:
                offenders[str(path)] = hits
    assert (
        offenders == {}
    ), f"发现疑似 SLO 红线常量（须等基线报告后以配置引入）: {offenders}"
