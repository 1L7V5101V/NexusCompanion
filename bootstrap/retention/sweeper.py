"""PG 行级 retention sweeper（p0-retention-wiring ADR-2/5/7/8）。

单轮编排：逐实体执行保留期裁剪并产出 `EntityReport`（字段集对齐
`core.telemetry.retention.SweepReport`，额外带可恢复性分类）。实体编排
规则（ADR-2 归属矩阵）：

- `webchat_replay_frames`：**按会话补发窗口**（消费游标 + 每会话下限 +
  兜底年龄天花板，ADR-8），可恢复（canonical 消息是真源，客户端可重建）。
- `tool_audit_events` / `admin_audit_events` / `work_attempts`：audit 档
  （默认 180d），不可恢复（追加型事实记录，§5.9.12）。
- 凭据摘要作废：`auth_sessions` / `access_tokens` 已撤销 ∧ 已过期 ∧ 超宽限
  → UPDATE digest = NULL（不删行），计不可恢复删除量（ADR-4）。
- `outbound_delivery_intents`（含 dead_letter）、canonical 消息/会话、
  `inbox_records`、`turns`、`attachments`、`background_work_items`：**显式
  不在任何删除清单**（不可删除集——负向测试逐一固化，见
  `tests/retention/test_no_delete_set.py`）。attachments 归 C6 自有生命周期
  （`retention_deadline`），避免两条腿删同一实体。
- 文件腿：仅 `config.retention.file_roots` 显式给出的 root 参与
  `sweep_roots`（mtime 判据只对按日期分片产物有效）；内置 root 集为空。
  **绝不**把 `workspace/logs/*.db`（活跃 SQLite 库）与
  `workspace/logs/tool_audit.ndjson`（单文件持续追加，mtime 永远最新）
  配为 root——对它们按 mtime 删除要么无效要么是数据销毁（ADR-1 Non-Goal）。

时间比较一律用数据库时钟（`now() - interval`，ADR-5：与 claim/now() 同源，
避免应用时钟漂移）。删除按 `ORDER BY <ts> ASC LIMIT batch_size` 分批，
单轮单实体最多 `max_batches` 批，超出留待下轮（幂等收敛）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import RetentionConfig
from bootstrap.db.repository.auth_repo import (
    AdminRepository,
    CredentialRepository,
)
from bootstrap.db.repository.control_plane_repo import (
    ToolAuditRepository,
    WebchatReplayRepository,
    WorkItemRepository,
)
from core.telemetry.redaction import redact_text
from core.telemetry.retention import RetentionPolicy, sweep_roots

logger = logging.getLogger(__name__)

RECOVERABLE = "recoverable"
"""派生数据：可从 canonical 消息等真源重建（补发缓冲）。"""

IRRECOVERABLE = "irrecoverable"
"""事实记录：删除即永久丢失（审计流水、凭据摘要作废、观测分片文件）。"""

_SECONDS_PER_DAY = 86400


@dataclass
class EntityReport:
    """单实体单轮报告（字段集 = SweepReport + recoverability 分类）。"""

    category: str
    """retention 归属：三档之一或 `session_replay`（按会话补发窗口，非全局年龄档）。"""

    target: str
    """存储标识：PG 表名或文件 root。"""

    scanned: int = 0
    deleted: int = 0
    kept: int = 0
    bytes_freed: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    recoverability: str = IRRECOVERABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "target": self.target,
            "scanned": self.scanned,
            "deleted": self.deleted,
            "kept": self.kept,
            "bytes_freed": self.bytes_freed,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
            "recoverability": self.recoverability,
        }


@dataclass
class RetentionRunReport:
    """一轮执行的汇总（含可恢复性分类合计，ADR-7）。"""

    dry_run: bool = False
    started_at: str = ""
    entities: list[EntityReport] = field(default_factory=list)

    @property
    def recoverable_deleted(self) -> int:
        return sum(e.deleted for e in self.entities if e.recoverability == RECOVERABLE)

    @property
    def irrecoverable_deleted(self) -> int:
        return sum(
            e.deleted for e in self.entities if e.recoverability == IRRECOVERABLE
        )

    @property
    def recoverable_bytes(self) -> int:
        return sum(
            e.bytes_freed for e in self.entities if e.recoverability == RECOVERABLE
        )

    @property
    def irrecoverable_bytes(self) -> int:
        return sum(
            e.bytes_freed for e in self.entities if e.recoverability == IRRECOVERABLE
        )

    @property
    def ok(self) -> bool:
        return not any(e.errors for e in self.entities)

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "recoverable_deleted": self.recoverable_deleted,
            "irrecoverable_deleted": self.irrecoverable_deleted,
            "recoverable_bytes": self.recoverable_bytes,
            "irrecoverable_bytes": self.irrecoverable_bytes,
            "ok": self.ok,
            "entities": [e.to_dict() for e in self.entities],
        }


class RetentionSweeper:
    """保留期执行的单轮编排（dry_run=True 时零变更，只报告）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        config: RetentionConfig,
    ) -> None:
        self._sf = session_factory
        self._config = config
        self._replay = WebchatReplayRepository(session_factory)
        self._tool_audit = ToolAuditRepository(session_factory)
        self._work_items = WorkItemRepository(session_factory)
        self._admin = AdminRepository(session_factory)
        self._credentials = CredentialRepository(session_factory)

    async def run_once(self, *, dry_run: bool = False) -> RetentionRunReport:
        cfg = self._config
        report = RetentionRunReport(
            dry_run=dry_run,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        report.entities.append(await self._sweep_replay_frames(dry_run=dry_run))
        report.entities.append(
            await self._sweep_audit_table(
                category="audit",
                target="tool_audit_events",
                repo_stats=self._tool_audit.count_audit_rows,
                repo_count_expired=self._tool_audit.count_expired_audit,
                repo_delete_batch=self._tool_audit.delete_expired_audit_batch,
                dry_run=dry_run,
            )
        )
        report.entities.append(
            await self._sweep_audit_table(
                category="audit",
                target="admin_audit_events",
                repo_stats=self._admin.count_admin_audit_rows,
                repo_count_expired=self._admin.count_expired_admin_audit,
                repo_delete_batch=self._admin.delete_expired_admin_audit_batch,
                dry_run=dry_run,
            )
        )
        report.entities.append(
            await self._sweep_audit_table(
                category="audit",
                target="work_attempts",
                repo_stats=self._work_items.count_work_attempt_rows,
                repo_count_expired=self._work_items.count_expired_work_attempts,
                repo_delete_batch=self._work_items.delete_expired_work_attempts_batch,
                dry_run=dry_run,
            )
        )
        report.entities.append(
            await self._sweep_credentials(dry_run=dry_run)
        )
        report.entities.extend(self._sweep_files(dry_run=dry_run))
        for entity in report.entities:
            if entity.deleted or entity.errors:
                logger.info(
                    "retention %s target=%s scanned=%d deleted=%d kept=%d "
                    "bytes=%d dry_run=%s recoverability=%s errors=%d",
                    entity.category,
                    entity.target,
                    entity.scanned,
                    entity.deleted,
                    entity.kept,
                    entity.bytes_freed,
                    entity.dry_run,
                    entity.recoverability,
                    len(entity.errors),
                )
        return report

    # ── 实体编排 ────────────────────────────────────────────────

    async def _sweep_replay_frames(self, *, dry_run: bool) -> EntityReport:
        """重放帧：逐会话补发窗口裁剪（ADR-8），可恢复（派生缓冲）。"""
        cfg = self._config
        report = EntityReport(
            category="session_replay",
            target="webchat_replay_frames",
            dry_run=dry_run,
            recoverability=RECOVERABLE,
        )
        try:
            total_frames, total_bytes = await self._replay.count_replay_frames()
            report.scanned = total_frames
            deleted = 0
            bytes_freed = 0
            conversations = await self._replay.list_frame_conversations()
            for tenant_id, conversation_id in conversations:
                for _ in range(max(1, cfg.max_batches)):
                    d, b = await self._replay.delete_frames_window_batch(
                        tenant_id,
                        conversation_id,
                        keep_last_frames=cfg.replay_keep_last_frames,
                        max_age_s=cfg.replay_max_age_days * _SECONDS_PER_DAY,
                        batch_size=cfg.batch_size,
                        dry_run=dry_run,
                    )
                    deleted += d
                    bytes_freed += b
                    if d < cfg.batch_size:
                        break
            report.deleted = deleted
            report.bytes_freed = bytes_freed
            report.kept = max(0, report.scanned - deleted)
        except Exception as exc:
            report.errors.append(redact_text(f"{type(exc).__name__}: {exc}"))
            logger.exception("retention replay frames sweep 失败（下轮继续）")
        return report

    async def _sweep_audit_table(
        self,
        *,
        category: str,
        target: str,
        repo_stats,
        repo_count_expired,
        repo_delete_batch,
        dry_run: bool,
    ) -> EntityReport:
        """审计类实体：全局年龄档分批删除（ADR-5），不可恢复。"""
        cfg = self._config
        report = EntityReport(
            category=category,
            target=target,
            dry_run=dry_run,
            recoverability=IRRECOVERABLE,
        )
        older_than_s = cfg.audit_days * _SECONDS_PER_DAY
        cap = cfg.batch_size * max(1, cfg.max_batches)
        try:
            report.scanned = await repo_stats()
            if dry_run:
                deleted, bytes_freed = await repo_count_expired(older_than_s, cap)
            else:
                deleted = 0
                bytes_freed = 0
                for _ in range(max(1, cfg.max_batches)):
                    d, b = await repo_delete_batch(older_than_s, cfg.batch_size)
                    deleted += d
                    bytes_freed += b
                    if d < cfg.batch_size:
                        break
            report.deleted = deleted
            report.bytes_freed = bytes_freed
            report.kept = max(0, report.scanned - deleted)
        except Exception as exc:
            report.errors.append(redact_text(f"{type(exc).__name__}: {exc}"))
            logger.exception("retention %s sweep 失败（下轮继续）", target)
        return report

    async def _sweep_credentials(self, *, dry_run: bool) -> EntityReport:
        """凭据摘要作废（ADR-4）：UPDATE 置 NULL 不删行；计不可恢复删除量。"""
        cfg = self._config
        report = EntityReport(
            category="credentials",
            target="auth_sessions+access_tokens",
            dry_run=dry_run,
            recoverability=IRRECOVERABLE,
        )
        try:
            sessions, tokens = await self._credentials.count_credential_rows()
            report.scanned = sessions + tokens
            cap = cfg.batch_size * max(1, cfg.max_batches)
            if dry_run:
                cleared = await self._credentials.count_purgeable_credentials(
                    grace_s=cfg.purge_grace_s, cap=cap
                )
            else:
                cleared = 0
                for _ in range(max(1, cfg.max_batches)):
                    n = await self._credentials.purge_expired_credential_digests(
                        grace_s=cfg.purge_grace_s, batch_size=cfg.batch_size
                    )
                    cleared += n
                    if n < cfg.batch_size:
                        break
            report.deleted = cleared
            report.kept = max(0, report.scanned - cleared)
        except Exception as exc:
            report.errors.append(redact_text(f"{type(exc).__name__}: {exc}"))
            logger.exception("retention credentials purge 失败（下轮继续）")
        return report

    def _sweep_files(self, *, dry_run: bool) -> list[EntityReport]:
        """文件腿（ADR-1）：只清 config 显式给出的 root；内置 root 集为空。

        未配置任何 root 时输出一条 scanned=0 占位报告（文件腿存在但零动作），
        而不是静默缺席——演练报告要能看出"文件腿跑过且什么都没删"。
        """
        cfg = self._config
        if not cfg.file_roots:
            return [
                EntityReport(
                    category="files",
                    target="(no file_roots configured)",
                    scanned=0,
                    dry_run=dry_run,
                )
            ]
        policy = RetentionPolicy(
            operational_days=cfg.operational_days,
            audit_days=cfg.audit_days,
            debug_content_days=cfg.debug_content_days,
        )
        reports: list[EntityReport] = []
        try:
            swept = sweep_roots(cfg.file_roots, policy, dry_run=dry_run)
        except Exception as exc:
            report = EntityReport(
                category="files",
                target="file_roots",
                dry_run=dry_run,
            )
            report.errors.append(redact_text(f"{type(exc).__name__}: {exc}"))
            return [report]
        for item in swept:
            reports.append(
                EntityReport(
                    category=item.category,
                    target=str(item.root),
                    scanned=item.scanned,
                    deleted=item.deleted,
                    kept=item.kept,
                    bytes_freed=item.bytes_freed,
                    dry_run=dry_run,
                    errors=[redact_text(e) for e in item.errors],
                    recoverability=IRRECOVERABLE,
                )
            )
        return reports


__all__ = ["IRRECOVERABLE", "RECOVERABLE", "EntityReport", "RetentionRunReport", "RetentionSweeper"]
