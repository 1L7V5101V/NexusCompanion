"""dry_run 场景：验证 harness 骨架（CLI → 场景注册 → 结果 JSON 写出）而不执行真实 turn。

只校验输入参数回写与统计结构：stats 为空（attempted=0），spec.dry_run 必须为 True。
2.4 起的真实场景复用同一 LoadSpec / RunStats 口径与场景签名。
"""

from __future__ import annotations

from scripts.load.result import LoadSpec, RunStats
from scripts.load.scenarios import LoadDeps, register


@register("dry_run")
async def run(spec: LoadSpec, deps: LoadDeps) -> dict[str, RunStats]:
    if not spec.dry_run:
        raise RuntimeError("dry_run 场景要求 --dry-run 标志")
    return {spec.backend: RunStats(backend=spec.backend)}
