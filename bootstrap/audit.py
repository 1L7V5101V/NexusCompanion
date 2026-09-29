"""C7 task 7.1：tool_audit_events 双 adapter 装配（design ADR-6）。

- 多租户 PG（auth.enabled ∧ storage.backend=postgres）：control-plane 表追加
  INSERT（``tool_audit_events``，alembic b3f7a1c5d9e2）；
- 其余（单机/开发，含 SQLite）：同字段结构化 JSON 行兜底
  （``workspace/logs/tool_audit.ndjson``）。
写入均 fail-open：审计失败只告警，不阻断工具调用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent.admission.tool_audit import LogAuditSink, ToolAuditEvent
from bootstrap.db.repository.control_plane_repo import ToolAuditRepository
from bootstrap.work_queue import async_pg_url

logger = logging.getLogger(__name__)


class PgToolAuditSink:
    """control-plane PG 追加写入（审计流，无 CAS/回填语义）。

    E10 tool_call 记录点（pg-durable-sot-cutover task 6.1）：审计写入后以
    `ToolAuditEvent` 字段投影一条 lifecycle 事件（白名单构造，同 fixture）；
    记录点失败不影响审计写入（各自 fail-open）。
    """

    def __init__(self, repo: ToolAuditRepository, telemetry: Any = None) -> None:
        self._repo = repo
        self._telemetry = telemetry

    async def write(self, event: ToolAuditEvent) -> None:
        try:
            await self._repo.record_event(event)
        except Exception:  # noqa: BLE001 —— 审计失败不阻断工具调用
            logger.warning(
                "tool_audit PG 写入失败 tenant=%s tool=%s",
                event.tenant_id,
                event.tool_name,
                exc_info=True,
            )
        if self._telemetry is not None:
            try:
                self._telemetry.tool_call_finished(
                    tool_call_id=event.tool_call_id,
                    turn_id=event.turn_id or None,
                    tenant_id=event.tenant_id,
                    tool_name=event.tool_name,
                    result=event.status,
                    duration_ms=event.duration_ms,
                    error_code=event.error_code,
                    effect_class=event.effect_class,
                )
            except Exception:  # noqa: BLE001 —— 记录点失败不阻断
                logger.warning(
                    "tool_call lifecycle 记录点失败 tool=%s",
                    event.tool_name,
                    exc_info=True,
                )


def resolve_tool_audit_sink(
    *,
    multi_tenant: bool,
    backend: str,
    postgres_url: str,
    workspace: Path,
) -> tuple[Any, AsyncEngine | None]:
    """按存储后端选择审计适配器；返回 ``(sink, engine)``（PG 时 engine 非空）。

    engine 为进程级控制面连接（Pilot 单进程；停机随进程退出释放）。
    """
    if multi_tenant and backend == "postgres":
        engine = create_async_engine(
            async_pg_url(postgres_url),
            pool_pre_ping=True,
        )
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        from bootstrap.webchat_telemetry import build_default_lifecycle_telemetry

        try:
            telemetry = build_default_lifecycle_telemetry()
        except Exception:  # noqa: BLE001 —— 指标注册失败降级为无遥测
            telemetry = None
        return PgToolAuditSink(ToolAuditRepository(factory), telemetry), engine
    return LogAuditSink(workspace / "logs"), None