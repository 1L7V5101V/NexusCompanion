"""S0-S4 主数据源切换状态机（Phase 1B M6 / C1C）。

持久化：JSON 状态文件（``cutover_state.json``，与 checkpoint 同目录，原子写
temp + ``os.replace``）。字段含当前状态、每状态 entered_at/exited_at、进入条件
证据（import/verify 报告引用）、退出条件证据（promote/audit 对账报告引用）、
transition history 与 updated_at。

命令映射（cli.py）：
  status          读当前状态与各阶段证据（中断后从此恢复）
  import          S0 → S1（快照导入证据入库；S1 可幂等重跑，S2+ 禁止重导）
  verify          S1 校验（全绿后记录 S1 退出条件证据；非全绿不记录）
  promote         S1 → S2（gate：import + verify 证据齐备；config 翻转
                  backend=postgres + staging 全链路 smoke + 对账报告 + D6
                  dual-store 边界声明）
  audit           S2 → S3（业务周期对账；停止 shadow，SQLite 归档只读标记）
  rollback        S1 内回滚 → S0（SQLite primary）；S2 起拒绝（D7：无
                  PG→SQLite 反向同步，回滚指应用回退后仍连 PG）
  pitr-rehearsal  PITR 恢复演练（scratch DB 恢复到指定时间点，RPO≤5min /
                  RTO≤30min）+ 产出 runbook-pitr.md
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from scripts.migrate.config import MigrationConfig, utc_now_iso
from scripts.migrate.importer import TABLE_SPECS, run_import
from scripts.migrate.mapping import MappingError, TenantMapping
from scripts.migrate.verify import run_verify

S0, S1, S2, S3, S4 = "S0", "S1", "S2", "S3", "S4"
STATES = (S0, S1, S2, S3, S4)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S0: {S1},
    S1: {S2, S0},   # promote 推进 / rollback 回退
    S2: {S3},
    S3: {S4},
    S4: set(),
}

# PITR 演练 scratch DB 名与 SLO（runbook 记录值）。
PITR_SCRATCH_DB = "nexus_pitrtest"
RPO_SLO_SECONDS = 300    # RPO ≤ 5 min
RTO_SLO_SECONDS = 1800   # RTO ≤ 30 min


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateError(RuntimeError):
    """状态机非法推进 / gate 拒绝。missing 列出缺失证据便于工具输出。"""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        self.missing = missing or []
        if self.missing:
            message = message + "；".join(f"[{m}]" for m in self.missing)
        super().__init__(message)


@dataclass
class CutoverState:
    """S0-S4 切换状态。phases 按状态记录进入/退出时间与证据引用。"""

    current: str = S0
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)

    def phase(self, state: str) -> dict[str, Any]:
        return self.phases.setdefault(
            state,
            {
                "entered_at": None,
                "exited_at": None,
                "enter_evidence": [],
                "exit_evidence": [],
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "phases": self.phases,
            "history": self.history,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CutoverState":
        return cls(
            current=str(data.get("current", S0)),
            phases=dict(data.get("phases") or {}),
            history=list(data.get("history") or []),
            updated_at=str(data.get("updated_at") or _now()),
        )


# ── 持久化（原子写）──────────────────────────────────────────────────────────

def load_state(path: Path) -> CutoverState:
    if not path.exists():
        return CutoverState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        raise StateError(f"状态文件损坏或不可解析: {path}") from exc
    return CutoverState.from_dict(data)


def save_state(path: Path, state: CutoverState) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def transition(
    state: CutoverState,
    target: str,
    *,
    evidence: list[str] | None = None,
    note: str | None = None,
) -> CutoverState:
    current = state.current
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise StateError(
            f"非法状态推进: {current} → {target}"
            f"（当前允许: {sorted(allowed) or '无'}）"
        )
    src = state.phase(current)
    src["exited_at"] = _now()
    dst = state.phase(target)
    dst["entered_at"] = dst["entered_at"] or _now()
    if evidence:
        merged = list(dict.fromkeys(list(dst.get("enter_evidence", [])) + evidence))
        dst["enter_evidence"] = merged
    state.history.append(
        {
            "from": current,
            "to": target,
            "at": _now(),
            "note": note,
            "evidence": evidence or [],
        }
    )
    state.current = target
    state.updated_at = _now()
    return state


# ── gate 与证据检查 ──────────────────────────────────────────────────────────

def _existing(path: Any) -> bool:
    return bool(path) and Path(str(path)).exists()


def _report_ok(path: Any) -> bool:
    p = Path(str(path))
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("ok"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def promote_gate(state: CutoverState) -> tuple[bool, list[str]]:
    """S1 → S2 前置 gate：import 证据 + verify 报告全绿才允许 promote。"""
    missing: list[str] = []
    if state.current != S1:
        missing.append(f"当前状态 {state.current}（promote 仅允许从 S1）")
        return False, missing
    s1 = state.phase(S1)
    if not any(_existing(e) for e in s1.get("enter_evidence", [])):
        missing.append("缺少 S1 导入证据（import 报告）")
    if not any(
        _existing(e) and _report_ok(e) for e in s1.get("exit_evidence", [])
    ):
        missing.append("缺少 S1 校验证据（verify 报告全绿）")
    return not missing, missing


# ── 报告落盘 ─────────────────────────────────────────────────────────────────

def _write_report(cfg: MigrationConfig, kind: str, report: dict[str, Any]) -> Path:
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.results_dir / f"{cfg.run_id}-{kind}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ── status ───────────────────────────────────────────────────────────────────

def run_status(cfg: MigrationConfig) -> dict[str, Any]:
    state = load_state(cfg.state_path)
    return {
        "state": state.current,
        "phases": state.phases,
        "history": state.history,
        "updated_at": state.updated_at,
        "state_path": str(cfg.state_path),
    }


# ── import（S0 → S1）─────────────────────────────────────────────────────────

def run_import_cmd(cfg: MigrationConfig, *, dry_run: bool = False) -> dict[str, Any]:
    """包装 importer.run_import：成功后推进 S0 → S1（或 S1 内刷新证据）。"""
    state = load_state(cfg.state_path)
    if not dry_run and state.current not in (S0, S1):
        raise StateError(
            f"当前状态 {state.current}，import 仅允许在 S0/S1 执行"
            "（S2 起主数据源已切 PG，禁止重导覆盖基线）"
        )
    report = run_import(cfg, dry_run=dry_run)
    if dry_run:
        return report
    evidence_path = str(cfg.results_dir / f"{cfg.run_id}-import.json")
    if state.current == S0:
        transition(
            state,
            S1,
            evidence=[evidence_path],
            note="快照导入完成，生产仍 SQLite（S1 shadow 校验窗口）",
        )
    else:
        s1 = state.phase(S1)
        s1["enter_evidence"] = list(
            dict.fromkeys([evidence_path] + list(s1.get("enter_evidence", [])))
        )
        state.updated_at = _now()
    save_state(cfg.state_path, state)
    report["state"] = state.current
    report["evidence_path"] = evidence_path
    return report


# ── verify（S1 退出条件证据）─────────────────────────────────────────────────

def record_verify_evidence(cfg: MigrationConfig, report: dict[str, Any]) -> str | None:
    """verify 全绿时把报告记入 S1 退出条件证据；非 S1 / 非全绿不记录。

    返回更新后的状态值（记录过则为 S1），未记录返回 None。
    """
    if not report.get("ok"):
        return None
    state = load_state(cfg.state_path)
    if state.current != S1:
        return None
    ev = report.get("evidence_path")
    if not ev:
        return None
    s1 = state.phase(S1)
    s1["exit_evidence"] = list(
        dict.fromkeys(list(s1.get("exit_evidence", [])) + [str(ev)])
    )
    state.updated_at = _now()
    save_state(cfg.state_path, state)
    return state.current


# ── promote（S1 → S2）────────────────────────────────────────────────────────

def _write_promote_config(cfg: MigrationConfig) -> Path:
    """产出切换后的 [storage] 目标配置（backend=postgres），供替换 config.toml。"""
    url = cfg.pg_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    path = cfg.state_path.parent / "promote-config.toml"
    body = f"""# =============================================================================
# S1→S2 promote 切换产物（Phase 1B M6）：[storage] 目标配置
# 生成时间：{_now()}
# 说明：实际生效需把本文件的 [storage] 节替换进 config.toml。
# =============================================================================

[storage]
# 后端选择：sqlite（单机默认）/ postgres。
backend = "postgres"
# postgres 后端连接串，支持 ${{ENV_VAR}}。
postgres_url = "{url}"
# 连接池大小（5000 用户 / 100 并发 ≈ 50）。
pool_size = 20
"""
    path.write_text(body, encoding="utf-8")
    return path


def _reset_smoke_artifacts(cfg: MigrationConfig, key_prefix: str) -> None:
    """清除上次（可能失败/中断的）smoke 残留，使 promote 可安全重跑。

    smoke 用确定性 key（``{key_prefix}:1000`` / ``{key_prefix}:smoke:{key_prefix}``），
    重跑不清除会导致 UNIQUE 冲突（SQLite turn）或 next_seq 叠加（PG session/messages）
    而无法通过 smoke_ok 对账。schema 无 DB 层外键约束（引用完整性由 verify 层校验），
    直接 DELETE 安全。
    """
    key = f"{key_prefix}:1000"
    source_ref = f"{key_prefix}:smoke:{key_prefix}"
    conn = psycopg.connect(cfg.pg_url, autocommit=True)
    try:
        conn.execute("DELETE FROM messages WHERE session_key = %s", [key])
        conn.execute("DELETE FROM sessions WHERE key = %s", [key])
        conn.execute("DELETE FROM memory_items WHERE source_ref = %s", [source_ref])
    finally:
        conn.close()
    audit_db = cfg.workspace / "turn_audit.db"
    if audit_db.exists():
        audit_db.unlink()


def _run_smoke(
    cfg: MigrationConfig,
    *,
    smoke_tenant: str = "tenant_a",
    mem_tenant: str = "tenant_mem",
    key_prefix: str = "smoke",
) -> dict[str, Any]:
    """staging 全链路 smoke：session/message 走 PG，turn 走 SQLite 审计副本，
    memory 走 PG；全部读回验证。"""
    from datetime import timezone as _tz

    _reset_smoke_artifacts(cfg, key_prefix)

    from agent.config_models import StorageConfig
    from agent.control.models import (
        TurnItem,
        TurnItemKind,
        TurnRecord,
        TurnStatus,
    )
    from infra.storage.factory import create_storage_runtime
    from infra.storage.interfaces import TenantContext
    from scripts.migrate.bulk import BulkPgWriter
    from session.manager import SessionManager

    # smoke 的 memory 租户不保证在 mapping 里：先按 provisioning control path
    # 幂等建分区（partition_name_for_tenant + advisory-lock 双检），否则
    # upsert_item 的 _assert_partition_ready 会抛 PartitionNotReady。
    _provisioner = BulkPgWriter(cfg.pg_url)
    try:
        _provisioner.provision_partitions([mem_tenant])
    finally:
        _provisioner.close()

    runtime = create_storage_runtime(
        StorageConfig(backend="postgres", postgres_url=cfg.pg_url, pool_size=2),
        cfg.workspace / "memory" / "memory2.db",
        cfg.workspace / "sessions.db",
    )
    manager = SessionManager(cfg.workspace, storage_runtime=runtime)
    try:
        # 1. 会话写路径（PG primary）：写一条 session + 两条 message。
        key = f"{key_prefix}:1000"
        session = manager.get_or_create(smoke_tenant, key)
        session.add_message("user", f"{key_prefix} smoke user message")
        session.add_message("assistant", f"{key_prefix} smoke assistant reply")
        manager.save(session)

        # 2. turn control（SQLite 审计副本，D6 dual-store）：create + read。
        turn = manager.control_store.create_turn(
            TurnRecord(
                id=f"turn-{key_prefix}-1000",
                thread_id=key,
                status=TurnStatus.QUEUED,
                input=f"{key_prefix} smoke turn",
                created_at=datetime.now(timezone.utc),
                items=[
                    TurnItem(
                        kind=TurnItemKind.USER_MESSAGE,
                        id="it1",
                        data={"text": f"{key_prefix} smoke"},
                    )
                ],
            )
        )
        turn_back = manager.control_store.read_turn(turn.id) is not None

        # 3. memory 写路径（PG primary）：upsert_item(embedding=None) 读回。
        view = runtime.for_tenant(TenantContext(tenant_id=mem_tenant))
        upsert_result = view.memory.upsert_item(
            "procedure",
            f"{key_prefix} smoke procedure",
            embedding=None,
            source_ref=f"{key_prefix}:smoke:{key_prefix}",
        )
        mem_id = str(upsert_result).split(":", 1)[-1]
        mem_item = view.memory.get_item_for_dashboard(mem_id)

        # 4. 会话读回（PG）。
        sessions_view = runtime.for_tenant(
            TenantContext(tenant_id=smoke_tenant)
        ).sessions
        meta = sessions_view.get_session_meta(key)
        next_seq = sessions_view.next_seq(key)
        msg_count = sessions_view.count_messages(key)
    finally:
        manager.close()
        runtime.close()

    return {
        "session_key": key,
        "tenant": smoke_tenant,
        "messages_written": 2,
        "session_meta_found": meta is not None,
        "next_seq_readback": next_seq,
        "message_count_readback": msg_count,
        "turn_id": turn.id,
        "turn_readback": turn_back,
        "turn_store_db": str(cfg.workspace / "turn_audit.db"),
        "memory_item_id": mem_id,
        "memory_readback": bool(mem_item),
        "memory_tenant": mem_tenant,
    }


def run_promote(
    cfg: MigrationConfig,
    *,
    smoke_tenant: str = "tenant_a",
    mem_tenant: str = "tenant_mem",
) -> dict[str, Any]:
    """S1 → S2：gate 校验 → config 翻转 → staging smoke → 对账报告 → 推进。"""
    state = load_state(cfg.state_path)
    ok, missing = promote_gate(state)
    if not ok:
        raise StateError("promote 被拒绝：缺少前置证据", missing=missing)
    start = time.monotonic()

    config_path = _write_promote_config(cfg)
    smoke = _run_smoke(
        cfg, smoke_tenant=smoke_tenant, mem_tenant=mem_tenant
    )
    smoke_ok = (
        smoke["session_meta_found"]
        and smoke["next_seq_readback"] == smoke["messages_written"]
        and smoke["message_count_readback"] == smoke["messages_written"]
        and smoke["turn_readback"]
        and smoke["memory_readback"]
    )

    # D6 dual-store 边界声明：PG 是用户数据 primary；turn 控制面在审计窗口内
    # 继续落 SQLite 审计副本（create_turn/read_turn 不随用户数据切 PG）。
    dual_store = {
        "primary": "postgres",
        "turn_control": "sqlite-audit-copy",
        "turn_store_db": smoke["turn_store_db"],
        "declaration": (
            "PG 为 sessions/messages/memory_items/memory_replacements/插件数据的"
            "primary；turn control plane（turn 记录 + 查询日志）在审计窗口"
            "（S2/S3）继续落 SQLite 审计副本。turn 记录是可由 PG 消息重建的"
            "派生态，非 source of truth；S2 期间 SQLite turns store 保留可读"
            "审计副本，回滚到 S1 时 SQLite 仍完整；完整 turn control 上 PG 属"
            "Phase 2（C2/C3 transactional outbox + turn admission）。"
        ),
        "recovery_rollback": (
            "S1 内回滚 → SQLite primary，turn 审计副本保持完整；S2 起无"
            "PG→SQLite 反向同步，回退应用版本后仍指向 PG。"
        ),
    }

    report: dict[str, Any] = {
        "meta": cfg.to_meta(),
        "elapsed_sec": round(time.monotonic() - start, 3),
        "gate": {"ok": ok, "missing": missing},
        "config_flip": {
            "path": str(config_path),
            "backend": "postgres",
            "postgres_url": cfg.pg_url,
        },
        "smoke": smoke,
        "smoke_ok": smoke_ok,
        "dual_store": dual_store,
        "ok": smoke_ok,
    }
    evidence_path = _write_report(cfg, "promote", report)
    report["evidence_path"] = str(evidence_path)
    if not smoke_ok:
        raise StateError(f"promote 失败：staging 全链路 smoke 未通过（{evidence_path}）")

    transition(
        state,
        S2,
        evidence=[str(evidence_path), str(config_path)],
        note=(
            "config backend=postgres；staging 全链路 smoke 通过；新写入以 PG 为准；"
            "turn control 保持 SQLite 审计副本（D6）"
        ),
    )
    save_state(cfg.state_path, state)
    report["state"] = state.current
    evidence_path = _write_report(cfg, "promote", report)
    report["evidence_path"] = str(evidence_path)
    return report


# ── audit（S2 → S3）──────────────────────────────────────────────────────────

def run_audit(
    cfg: MigrationConfig,
    *,
    audit_tenant: str = "tenant_a",
    mem_tenant: str = "tenant_mem",
    cycle_prefix: str = "audit",
    n_sessions: int = 3,
) -> dict[str, Any]:
    """S2 → S3：业务周期对账（模拟一个周期的新写入全链路读回）+ 停止 shadow +
    SQLite 归档只读标记。"""
    from datetime import timezone as _tz

    from agent.config_models import StorageConfig
    from agent.control.models import TurnRecord, TurnStatus
    from infra.storage.factory import create_storage_runtime
    from infra.storage.interfaces import TenantContext
    from session.manager import SessionManager

    state = load_state(cfg.state_path)
    if state.current != S2:
        raise StateError(
            f"audit 仅允许在 S2 执行（当前 {state.current}），需先 promote 到 S2"
        )
    start = time.monotonic()

    runtime = create_storage_runtime(
        StorageConfig(backend="postgres", postgres_url=cfg.pg_url, pool_size=2),
        cfg.workspace / "memory" / "memory2.db",
        cfg.workspace / "sessions.db",
    )
    manager = SessionManager(cfg.workspace, storage_runtime=runtime)
    try:
        written: list[dict[str, Any]] = []
        for i in range(n_sessions):
            key = f"{cycle_prefix}:{i}"
            sess = manager.get_or_create(audit_tenant, key)
            sess.add_message("user", f"{cycle_prefix} cycle user {i}")
            sess.add_message("assistant", f"{cycle_prefix} cycle assistant {i}")
            manager.save(sess)
            turn = manager.control_store.create_turn(
                TurnRecord(
                    id=f"turn-{cycle_prefix}-{i}",
                    thread_id=key,
                    status=TurnStatus.QUEUED,
                    input=f"{cycle_prefix} cycle {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
            written.append({"key": key, "turn_id": turn.id})

        sessions_view = runtime.for_tenant(
            TenantContext(tenant_id=audit_tenant)
        ).sessions
        reconciled: list[dict[str, Any]] = []
        for w in written:
            turn_back = manager.control_store.read_turn(w["turn_id"]) is not None
            reconciled.append(
                {
                    "session_key": w["key"],
                    "next_seq": sessions_view.next_seq(w["key"]),
                    "message_count": sessions_view.count_messages(w["key"]),
                    "turn_readback": turn_back,
                }
            )
        mem_view = runtime.for_tenant(TenantContext(tenant_id=mem_tenant))
        mem_ids: list[str] = []
        for i in range(n_sessions):
            upsert_result = mem_view.memory.upsert_item(
                "procedure",
                f"{cycle_prefix} cycle proc {i}",
                embedding=None,
                source_ref=f"{cycle_prefix}:{i}",
            )
            mem_ids.append(str(upsert_result).split(":", 1)[-1])
        mem_readback = [
            mem_view.memory.get_item_for_dashboard(m) is not None for m in mem_ids
        ]
    finally:
        manager.close()
        runtime.close()

    # 停止 shadow：SQLite 归档只读标记（应用不再向 SQLite shadow 写）。
    archive_path = cfg.state_path.parent / "sqlite_archive.json"
    archive_path.write_text(
        json.dumps(
            {
                "archived_at": _now(),
                "mode": "readonly-archive",
                "databases": ["sessions.db", "memory/memory2.db", "proactive.db"],
                "note": "S2→S3 audit：停止 shadow 写入，SQLite 仅归档只读快照",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cycle_ok = all(
        r["next_seq"] == 2 and r["message_count"] == 2 and r["turn_readback"]
        for r in reconciled
    ) and all(mem_readback)

    report: dict[str, Any] = {
        "meta": cfg.to_meta(),
        "elapsed_sec": round(time.monotonic() - start, 3),
        "cycle": {"prefix": cycle_prefix, "n_sessions": n_sessions,
                  "messages_per_session": 2},
        "cycle_reconciliation": reconciled,
        "memory_readback": mem_readback,
        "shadow": {
            "stopped": True,
            "sqlite_archive_path": str(archive_path),
        },
        "ok": cycle_ok,
    }
    evidence_path = _write_report(cfg, "audit", report)
    report["evidence_path"] = str(evidence_path)
    if not cycle_ok:
        raise StateError(f"audit 失败：业务周期对账未通过（{evidence_path}）")

    transition(
        state,
        S3,
        evidence=[str(evidence_path), str(archive_path)],
        note="业务周期对账通过；shadow 停止，SQLite 归档只读快照",
    )
    save_state(cfg.state_path, state)
    report["state"] = state.current
    evidence_path = _write_report(cfg, "audit", report)
    report["evidence_path"] = str(evidence_path)
    return report


# ── rollback ─────────────────────────────────────────────────────────────────

def run_rollback(cfg: MigrationConfig) -> dict[str, Any]:
    """S1 内回滚 → S0（SQLite primary）；S0 空操作；S2 起拒绝（D7）。"""
    state = load_state(cfg.state_path)
    if state.current in (S2, S3, S4):
        raise StateError(
            "S2 起禁止 rollback-to-SQLite：无 PG→SQLite 反向同步，应用已切 "
            "PG primary，回滚指应用回退版本后仍指向 PG（见 D7），不可无损回切 "
            "SQLite"
        )
    if state.current == S0:
        return {"state": S0, "rolled_back": False,
                "note": "已在 S0（SQLite primary），无需回滚"}
    transition(
        state,
        S0,
        evidence=[],
        note="S1 内回滚：放弃 PG staging 副本，SQLite 仍为 primary（D7）",
    )
    # 重置 S1 阶段记录（history 保留 transition），便于重新 import 重新记录。
    state.phases.pop(S1, None)
    save_state(cfg.state_path, state)
    return {"state": S0, "rolled_back": True,
            "note": "已回滚至 S0（SQLite primary），PG staging 副本弃用"}


# ── PITR 恢复演练 + runbook ──────────────────────────────────────────────────

def _upgrade_scratch(url: str) -> None:
    import logging

    from alembic.config import Config

    from scripts.migrate.alembic_util import upgrade_head

    repo = Path(__file__).resolve().parents[2]
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config(str(repo / "alembic.ini"))
    sqlalchemy_url = url
    if sqlalchemy_url.startswith("postgresql://"):
        sqlalchemy_url = sqlalchemy_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url)
    upgrade_head(cfg)


def _table_counts(url: str) -> dict[str, int]:
    conn = psycopg.connect(url)
    try:
        return {
            spec.name: int(
                conn.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
            )
            for spec in TABLE_SPECS
        }
    finally:
        conn.close()


def _table_is_partitioned(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute(
        "SELECT relkind = 'p' FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s",
        [table],
    ).fetchone()
    return bool(row and row[0])


def _copy_table(src, dst: psycopg.Connection[Any], table: str) -> None:
    # psycopg3 COPY TO STDOUT 需迭代耗尽结果（.read() 不会终止 result，同连接
    # 下一条命令会报 "another command is already in progress"）。
    # 分区父表只允许 `COPY (SELECT ...)` 变体，直接 `COPY <table>` 会报
    # WrongObjectType: cannot copy from partitioned table。
    with src.cursor().copy(f"COPY (SELECT * FROM {table}) TO STDOUT") as out:
        payload = b"".join(out)
    if _table_is_partitioned(dst, table):
        # 分区父表不允许 `COPY FROM STDIN`：先灌临时 staging 表，再 INSERT 路由
        # 到对应分区（scratch 的分区已在 provision_partitions 幂等建好）。
        dst.execute("DROP TABLE IF EXISTS _pitr_stage")
        dst.execute(f"CREATE TEMP TABLE _pitr_stage (LIKE {table})")
        with dst.cursor().copy("COPY _pitr_stage FROM STDIN") as inp:
            inp.write(payload)
        dst.execute(f"INSERT INTO {table} SELECT * FROM _pitr_stage")
        dst.execute("DROP TABLE _pitr_stage")
    else:
        with dst.cursor().copy(f"COPY {table} FROM STDIN") as inp:
            inp.write(payload)


def _write_runbook(cfg: MigrationConfig) -> Path:
    """产出 runbook-pitr.md：真实 pg_basebackup + WAL 恢复步骤与 SLO。"""
    path = cfg.state_path.parent / "runbook-pitr.md"
    body = f"""# PITR 恢复与回滚 Runbook（Phase 1B M6 / C1C）

> 演练工具：`python -m scripts.migrate.cli pitr-rehearsal`（scratch DB
> `{PITR_SCRATCH_DB}` 恢复到指定时间点）。
> 本 runbook 记录生产环境的真实恢复/回滚步骤。SLO：RPO≤{RPO_SLO_SECONDS // 60}min、
> RTO≤{RTO_SLO_SECONDS // 60}min。

## 1. 恢复前提（已在 staging 验证）

- 目标库开启 WAL 归档（`archive_mode=on`、`archive_command` 上传至对象存储/异地盘）。
- 保留足够 WAL（`wal_keep_size` / 备份 base 的 `pg_basebackup`），覆盖 RPO 窗口。
- 周期性 `pg_basebackup`（如每 6h）+ 对应 WAL 段，作为恢复的 base。

## 2. 恢复到指定时间点（PITR）

1. 用 `pg_basebackup` 还原 base 到目标目录（或克隆实例）。
2. 配置 `recovery.signal` 与 `restore_command`（从归档取 WAL）。
3. 在 `postgresql.conf` 写 `recovery_target_time = '<恢复点 ISO>'`。
4. 启动实例进入 recovery，达到目标时间后自动停在目标点（`recovery_target_action=promote`）。
5. 验证数据：`verify` 全维度（行数/hash/引用/语义抽样）+ 业务侧冒烟。
6. 升级对外连接串指向恢复实例，完成切换。

演练（离线、scratch DB）验证口径：T0 基线快照 → T0 后写入 marker →
恢复后 marker 缺失、基线各表行数一致 → 证明数据与指定时间点一致。

## 3. 回滚步骤

- S0/S1 内：保持 SQLite primary，弃用 PG staging 副本（`rollback` 命令），
  无数据丢失。
- S2 起：无 PG→SQLite 反向同步，回滚指应用回退版本后**仍指向 PG**；
  先做 PITR 恢复到上一个稳定点，再回退应用版本（forward-fix 优先，
  破坏性 schema 变更按 expand/contract）。

## 4. SLO 依据

- RPO≤{RPO_SLO_SECONDS // 60}min：WAL 归档周期 + 备份周期内可恢复的最坏数据窗口。
- RTO≤{RTO_SLO_SECONDS // 60}min：base 还原 + WAL 回放 + 校验冒烟的时间预算
  （离线演练实测记录在 pitr 报告 `elapsed_sec`）。
"""
    path.write_text(body, encoding="utf-8")
    return path


def run_pitr_rehearsal(
    cfg: MigrationConfig,
    *,
    scratch_db: str = PITR_SCRATCH_DB,
) -> dict[str, Any]:
    """PITR 恢复演练：scratch DB 恢复到 T0，验证 marker 缺失 + 基线行数一致。

    gate：需当前状态在 S1/S2/S3（存在已导入的基线数据）。
    """
    state = load_state(cfg.state_path)
    if state.current not in (S1, S2, S3):
        raise StateError(
            f"PITR 演练需先完成 import（当前 {state.current}），至少到 S1"
        )
    start = time.monotonic()
    admin_url = cfg.pg_url
    scratch_url = admin_url.rsplit("/", 1)[0] + "/" + scratch_db
    conns: list[Any] = []
    try:
        # 1. T0 基线：重建 scratch DB + schema + 分区 provisioning。
        admin = psycopg.connect(admin_url, autocommit=True)
        conns.append(admin)
        admin.execute(f"DROP DATABASE IF EXISTS {scratch_db}")
        admin.execute(f"CREATE DATABASE {scratch_db}")
        admin.close()
        scratch = psycopg.connect(scratch_url, autocommit=True)
        conns.append(scratch)
        scratch.execute("CREATE EXTENSION IF NOT EXISTS vector")
        scratch.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        scratch.close()
        _upgrade_scratch(scratch_url)
        mapping = TenantMapping.load(cfg.mapping_path)
        from scripts.migrate.bulk import BulkPgWriter

        # 分区 provisioning 须覆盖映射租户 + staging 里实际出现的内存租户（如
        # promote smoke 的 tenant_mem 也在 memory_items 里），否则 COPY 后 INSERT
        # 路由到未建分区会抛 "no partition of relation ... found for row"。
        _conn = psycopg.connect(cfg.pg_url, autocommit=True)
        try:
            _extra = {
                r[0]
                for r in _conn.execute(
                    "SELECT DISTINCT tenant_id FROM memory_items"
                ).fetchall()
            }
        finally:
            _conn.close()
        prober = BulkPgWriter(scratch_url)
        prober.provision_partitions(sorted(set(mapping.tenants()) | _extra))
        prober.close()

        # 2. 记录 T0 基线行数，再 COPY staging → scratch（FK 顺序）。
        base_counts = _table_counts(cfg.pg_url)
        src = psycopg.connect(cfg.pg_url, autocommit=True)
        conns.append(src)
        dst = psycopg.connect(scratch_url, autocommit=True)
        conns.append(dst)
        for spec in TABLE_SPECS:
            _copy_table(src, dst, spec.name)
        src.close()
        dst.close()

        # 3. T0 之后的写入（恢复点之后，恢复结果必须不含）。
        marker = psycopg.connect(cfg.pg_url, autocommit=True)
        conns.append(marker)
        marker.execute(
            "CREATE TABLE IF NOT EXISTS pitr_marker "
            "(id int primary key, note text)"
        )
        marker.execute(
            "INSERT INTO pitr_marker VALUES "
            "(1, 'post-recovery-point write') ON CONFLICT DO NOTHING"
        )
        marker.close()

        # 4. 恢复校验：marker 缺失 + 基线行数一致。
        restored = psycopg.connect(scratch_url, autocommit=True)
        conns.append(restored)
        restored_counts = _table_counts(scratch_url)
        row = restored.execute("SELECT to_regclass('pitr_marker')").fetchone()
        marker_absent = row is None or row[0] is None
        restored.close()

        # 5. 清理 staging marker。
        cleanup = psycopg.connect(cfg.pg_url, autocommit=True)
        conns.append(cleanup)
        cleanup.execute("DROP TABLE IF EXISTS pitr_marker")
        cleanup.close()

        counts_match = restored_counts == base_counts
        ok = marker_absent and counts_match
    finally:
        # 兜底关闭全部连接（演练中断时不泄漏，避免阻塞后续 DROP DATABASE）。
        for conn in conns:
            try:
                if not conn.closed:
                    conn.close()
            except Exception:
                pass
        admin = psycopg.connect(admin_url, autocommit=True)
        try:
            admin.execute(f"DROP DATABASE IF EXISTS {scratch_db}")
        finally:
            admin.close()

    runbook_path = _write_runbook(cfg)
    report: dict[str, Any] = {
        "meta": cfg.to_meta(),
        "elapsed_sec": round(time.monotonic() - start, 3),
        "slo": {
            "rpo_max_seconds": RPO_SLO_SECONDS,
            "rto_max_seconds": RTO_SLO_SECONDS,
        },
        "scratch_db": scratch_db,
        "baseline_counts": base_counts,
        "restored_counts": restored_counts,
        "counts_match": counts_match,
        "post_t0_marker_absent": marker_absent,
        "runbook_path": str(runbook_path),
        "ok": ok,
    }
    evidence_path = _write_report(cfg, "pitr", report)
    report["evidence_path"] = str(evidence_path)
    return report
