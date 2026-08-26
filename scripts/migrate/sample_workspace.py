"""生成多通道多租户样例 workspace（D2 基准数据，确定性）。

产出与真实源同构的 SQLite DB + JSON 配置文件，供批量导入、断点续传、
幂等、校验等任务使用：
  sessions.db         sessions / messages（含 turns 空表，Migration 不导入）
  memory/memory2.db   memory_items / consolidation_events / memory_replacements
  proactive.db        tick_log / tick_step_log / deliveries / session_state /
                      context_only_timestamps
  schedules.json / mcp_servers.json / proactive_quota.json

embedding 与 memory2.store 一致：``json.dumps`` 的 1024 维 float 列表 TEXT。
所有数据由固定 seed 的 RNG 生成，重复调用产出完全一致的文件。

用法:
    uv run python scripts/migrate/sample_workspace.py --out PATH [--messages N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VEC_DIM = 1024
BASE_TS = datetime(2026, 8, 1, tzinfo=timezone.utc)

CHANNELS = ["telegram", "discord", "whatsapp", "wechat", "slack"]
SESSIONS_PER_CHANNEL = 12
MEMORY_TYPES = ["fact", "preference", "event", "relationship"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _ts(i: int, j: int = 0) -> str:
    return _iso(BASE_TS + timedelta(minutes=i * 17 + j * 3))


def _embedding(rng: random.Random) -> str:
    return json.dumps([rng.gauss(0.0, 1.0) for _ in range(VEC_DIM)])


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _make_sessions_db(path: Path, rng: random.Random,
                      n_messages: int) -> list[tuple[str, str]]:
    """返回 [(session_key, channel)] 列表，供其它 DB 引用 session_key。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions ("
        " key TEXT PRIMARY KEY, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, last_consolidated INTEGER NOT NULL DEFAULT 0,"
        " metadata TEXT, last_user_at TEXT, last_proactive_at TEXT,"
        " next_seq INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE messages ("
        " id TEXT PRIMARY KEY, session_key TEXT NOT NULL, seq INTEGER NOT NULL,"
        " role TEXT NOT NULL, content TEXT, tool_chain TEXT, extra TEXT,"
        " ts TEXT NOT NULL, UNIQUE (session_key, seq))"
    )
    conn.execute(
        "CREATE TABLE turns ("
        " id TEXT PRIMARY KEY, session_key TEXT NOT NULL, status TEXT NOT NULL,"
        " input_json TEXT NOT NULL, items_json TEXT NOT NULL, usage_json TEXT,"
        " error_json TEXT, final_response TEXT, created_at TEXT NOT NULL,"
        " started_at TEXT, completed_at TEXT)"
    )
    conn.execute("CREATE INDEX idx_turns_session_created "
                 "ON turns(session_key, created_at, id)")

    sessions: list[tuple[str, str]] = []
    sid = 0
    for ch in CHANNELS:
        for _ in range(SESSIONS_PER_CHANNEL):
            key = f"{ch}:{1000 + sid}"
            conn.execute(
                "INSERT INTO sessions (key, created_at, updated_at,"
                " last_consolidated, metadata, last_user_at,"
                " last_proactive_at, next_seq) VALUES (?,?,?,?,?,?,?,?)",
                (key, _ts(sid * 10), _ts(sid * 10 + 5), 0,
                 json.dumps({"origin": "sample", "channel": ch}),
                 _ts(sid * 10 + 1), None, 0),
            )
            sessions.append((key, ch))
            sid += 1

    seq_of: dict[str, int] = {}
    for i in range(n_messages):
        key, _ = sessions[i % len(sessions)]
        seq = seq_of.get(key, 0) + 1
        seq_of[key] = seq
        role = "user" if seq % 2 == 1 else "assistant"
        content = (f"[{i}] sample message {i} in session {key}"
                   + (" long context filler " * (i % 3)))
        conn.execute(
            "INSERT INTO messages (id, session_key, seq, role, content,"
            " tool_chain, extra, ts) VALUES (?,?,?,?,?,?,?,?)",
            (f"m{i:07d}", key, seq, role, content, None, None, _ts(i)),
        )
    conn.commit()
    conn.close()
    return sessions


def _make_memory_db(path: Path, rng: random.Random,
                    sessions: list[tuple[str, str]], n_memory: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_items ("
        " id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, summary TEXT NOT NULL,"
        " content_hash TEXT NOT NULL, embedding TEXT,"
        " reinforcement INTEGER NOT NULL DEFAULT 1,"
        " emotional_weight INTEGER NOT NULL DEFAULT 0, extra_json TEXT,"
        " source_ref TEXT, happened_at TEXT,"
        " status TEXT NOT NULL DEFAULT 'active',"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("CREATE UNIQUE INDEX ux_items_hash "
                 "ON memory_items (content_hash, memory_type)")
    conn.execute(
        "CREATE TABLE consolidation_events ("
        " source_ref TEXT PRIMARY KEY, item_id TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE memory_replacements ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, old_item_id TEXT NOT NULL,"
        " old_memory_type TEXT NOT NULL, old_summary TEXT NOT NULL,"
        " old_source_ref TEXT, old_happened_at TEXT, old_extra_json TEXT,"
        " new_item_id TEXT NOT NULL, new_memory_type TEXT NOT NULL,"
        " new_summary TEXT NOT NULL, new_source_ref TEXT, new_happened_at TEXT,"
        " new_extra_json TEXT, relation_type TEXT NOT NULL DEFAULT 'supersede',"
        " source_ref TEXT, created_at TEXT NOT NULL)"
    )

    for i in range(n_memory):
        mtype = MEMORY_TYPES[i % len(MEMORY_TYPES)]
        summary = f"sample memory {i} [{mtype}] topic-{i % 37}"
        created = _ts(i, 1)
        conn.execute(
            "INSERT INTO memory_items (id, memory_type, summary, content_hash,"
            " embedding, reinforcement, emotional_weight, extra_json,"
            " source_ref, happened_at, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem{i:05d}", mtype, summary, _digest(summary),
             _embedding(rng), 1 + i % 5, i % 4, None, None, _ts(i, 2),
             "active", created, created),
        )

    # consolidation_events：source_ref 引用 messages.id
    for i in range(120):
        conn.execute(
            "INSERT INTO consolidation_events (source_ref, item_id, created_at)"
            " VALUES (?,?,?)",
            (f"m{i:07d}", f"mem{i % n_memory:05d}", _ts(i, 3)),
        )
    # memory_replacements：50 条，old/new 均引用 memory_items.id
    for i in range(50):
        conn.execute(
            "INSERT INTO memory_replacements (old_item_id, old_memory_type,"
            " old_summary, old_source_ref, old_happened_at, old_extra_json,"
            " new_item_id, new_memory_type, new_summary, new_source_ref,"
            " new_happened_at, new_extra_json, relation_type, source_ref,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem{i:05d}", MEMORY_TYPES[i % 4],
             f"old summary {i}", f"m{i:07d}", _ts(i, 4), None,
             f"mem{n_memory - i - 1:05d}", MEMORY_TYPES[(i + 1) % 4],
             f"new summary {i}", f"m{i + 120:07d}", _ts(i, 5), None,
             "supersede", f"m{i:07d}", _ts(i, 6)),
        )
    conn.commit()
    conn.close()


def _make_proactive_db(path: Path, sessions: list[tuple[str, str]],
                       n_ticks: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tick_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, tick_id TEXT NOT NULL,"
        " session_key TEXT NOT NULL, started_at TEXT NOT NULL,"
        " finished_at TEXT, gate_exit TEXT, terminal_action TEXT,"
        " skip_reason TEXT, steps_taken INTEGER, alert_count INTEGER,"
        " content_count INTEGER, context_count INTEGER, interesting_ids TEXT,"
        " discarded_ids TEXT, cited_ids TEXT, drift_entered INTEGER,"
        " final_message TEXT, proactive_effects_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE tick_step_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, tick_id TEXT NOT NULL,"
        " step_index INTEGER NOT NULL, phase TEXT, tool_name TEXT,"
        " tool_call_id TEXT, tool_args_json TEXT, tool_result_text TEXT,"
        " terminal_action_after TEXT, skip_reason_after TEXT,"
        " interesting_ids_after TEXT, discarded_ids_after TEXT,"
        " cited_ids_after TEXT, final_message_after TEXT)"
    )
    conn.execute(
        "CREATE TABLE deliveries ("
        " session_key TEXT NOT NULL, delivery_key TEXT NOT NULL,"
        " sent_at TEXT NOT NULL, PRIMARY KEY (session_key, delivery_key))"
    )
    conn.execute(
        "CREATE TABLE session_state ("
        " session_key TEXT NOT NULL, key TEXT NOT NULL, value TEXT,"
        " PRIMARY KEY (session_key, key))"
    )
    conn.execute(
        "CREATE TABLE context_only_timestamps ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL,"
        " ts TEXT NOT NULL)"
    )

    tick_ids: list[str] = []
    for i in range(n_ticks):
        key, _ = sessions[i % len(sessions)]
        tick_id = f"tick-{i:05d}"
        tick_ids.append(tick_id)
        conn.execute(
            "INSERT INTO tick_log (tick_id, session_key, started_at,"
            " finished_at, gate_exit, terminal_action, skip_reason,"
            " steps_taken, alert_count, content_count, context_count,"
            " interesting_ids, discarded_ids, cited_ids, drift_entered,"
            " final_message, proactive_effects_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tick_id, key, _ts(i, 7), _ts(i, 8),
             "pass" if i % 5 else "skip", None, None,
             1 + i % 3, i % 2, (i * 3) % 7, (i * 2) % 5,
             json.dumps(["mem0", "mem1"]), "[]",
             json.dumps(["mem2"]), i % 2, None, None),
        )
        if i % 3 == 0:
            for step in range(1 + i % 3):
                conn.execute(
                    "INSERT INTO tick_step_log (tick_id, step_index, phase,"
                    " tool_name, tool_call_id, tool_args_json,"
                    " tool_result_text, terminal_action_after,"
                    " skip_reason_after, interesting_ids_after,"
                    " discarded_ids_after, cited_ids_after,"
                    " final_message_after)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tick_id, step, "retrieve", "search_memory",
                     f"call-{i}-{step}", '{"query":"x"}',
                     "ok", None, None, "[]", "[]", "[]", None),
                )

    for i, (key, _) in enumerate(sessions):
        for j in range(2):
            conn.execute(
                "INSERT INTO deliveries (session_key, delivery_key, sent_at)"
                " VALUES (?,?,?)",
                (key, f"d-{i}-{j}", _ts(i, 9)),
            )
        conn.execute(
            "INSERT INTO session_state (session_key, key, value)"
            " VALUES (?,?,?)",
            (key, "last_proactive_at", _ts(i, 10)),
        )
        conn.execute(
            "INSERT INTO context_only_timestamps (session_key, ts)"
            " VALUES (?,?)", (key, _ts(i, 11)),
        )
    conn.commit()
    conn.close()


def _make_json_files(path: Path, sessions: list[tuple[str, str]]) -> None:
    jobs: list[dict[str, Any]] = []
    for i, (key, ch) in enumerate(sessions):
        if i % 4 != 0:
            continue
        chat = key.split(":", 1)[1]
        jobs.append({
            "id": f"job-{i:03d}", "trigger": "at", "tier": "instant",
            "fire_at": _ts(i, 12), "channel": ch, "chat_id": chat,
            "interval_seconds": None, "cron_expr": None,
            "message": f"reminder {i}", "prompt": None, "name": f"job {i}",
            "timezone": "Asia/Shanghai", "run_count": i % 3, "enabled": True,
        })
    (path / "schedules.json").write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (path / "mcp_servers.json").write_text(
        json.dumps({"servers": {
            "fs": {"command": "filesystem", "args": []},
            "http": {"url": "https://example.invalid/sse"},
        }}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (path / "proactive_quota.json").write_text(
        json.dumps({"enabled": True, "max_messages_per_day": 50,
                    "max_ticks_per_day": 10}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def generate_workspace(out: Path, *, n_messages: int = 10000,
                       n_memory: int = 2000, n_ticks: int = 60,
                       seed: int = 42) -> Path:
    """确定性生成样例 workspace，返回其路径。"""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    sessions = _make_sessions_db(out / "sessions.db", rng, n_messages)
    _make_memory_db(out / "memory" / "memory2.db", rng, sessions, n_memory)
    _make_proactive_db(out / "proactive.db", sessions, n_ticks)
    _make_json_files(out, sessions)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="生成迁移样例 workspace")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--messages", type=int, default=10000)
    parser.add_argument("--memory", type=int, default=2000)
    parser.add_argument("--ticks", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    generate_workspace(args.out, n_messages=args.messages, n_memory=args.memory,
                       n_ticks=args.ticks, seed=args.seed)
    print(f"sample workspace -> {args.out.resolve()}")


if __name__ == "__main__":
    main()
