"""SQLite → PostgreSQL 迁移工具链（Phase 1B，C1B/C1C）。

提供批量导入（COPY + 断点续传 + 幂等）、机器可读校验与 S0-S4 主数据源切换
状态机。设计见 openspec/changes/phase1b-migration-cutover/design.md D1-D7。
"""

from __future__ import annotations
