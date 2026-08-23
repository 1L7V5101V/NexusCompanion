"""可重复负载工具包（C0 任务 2.x）。

结构：
- harness.py    CLI 入口：参数解析、场景调度、结果 JSON 写出
- result.py     LoadSpec / TurnStats / RunStats / LoadResult 与统计口径
- scenarios/    场景注册表与场景脚本（dry_run 起步，2.4+ 加真实 turn 驱动）
"""

from __future__ import annotations
