#!/usr/bin/env python
"""PostgreSQL management helper for Akashic Agent.

Usage:
    python pg.py start       Start PostgreSQL
    python pg.py stop        Stop PostgreSQL
    python pg.py status      Check if PostgreSQL is running
    python pg.py init-db     Create nexus role/database + vector extension
    python pg.py psql        Open interactive psql shell
"""

import os
import subprocess
import sys
from pathlib import Path

PGROOT = Path("/d/Projects/postgres/pgsql")
PGDATA = Path("/d/Projects/postgres/data")
PG_BIN = PGROOT / "bin"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PG_BIN / f"{args[0]}.exe")] + list(args[1:]),
        env={**os.environ, "PGDATA": str(PGDATA)},
        capture_output=True,
        text=True,
    )


def start() -> None:
    if _is_running():
        print("PostgreSQL is already running.")
        return
    logfile = PGDATA / "postgres.log"
    result = _run("pg_ctl", "-D", str(PGDATA), "-l", str(logfile), "start")
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    else:
        print(f"PostgreSQL started. Log: {logfile}")


def stop() -> None:
    result = _run("pg_ctl", "-D", str(PGDATA), "stop")
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    else:
        print("PostgreSQL stopped.")


def status() -> None:
    result = _run("pg_isready")
    print(result.stdout.strip() or result.stderr.strip())


def psql() -> None:
    os.execv(str(PG_BIN / "psql.exe"), ["psql", "-U", "postgres", "-d", "nexus"])


def _psql(db: str, sql: str) -> subprocess.CompletedProcess:
    """以 postgres 超级用户对指定库执行 SQL，-At 便于读取查询输出。"""
    return subprocess.run(
        [
            str(PG_BIN / "psql.exe"),
            "-U", "postgres", "-d", db,
            "-v", "ON_ERROR_STOP=1",
            "-At", "-c", sql,
        ],
        env={**os.environ, "PGDATA": str(PGDATA)},
        capture_output=True,
        text=True,
    )


def _print_result(label: str, result: subprocess.CompletedProcess) -> None:
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(f"{label} 失败:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"{label} 完成。")


def init_db() -> None:
    """建 nexus 角色 / nexus 数据库 / vector 扩展（原生 PG 备用路径）。"""
    if not _is_running():
        print("PostgreSQL 未运行，先执行 `python pg.py start`。", file=sys.stderr)
        sys.exit(1)
    password = os.environ.get("POSTGRES_PASSWORD", "nexus_dev")
    role_sql = (
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus') THEN "
        f"CREATE ROLE nexus LOGIN PASSWORD '{password}'; END IF; END $$;"
    )
    _print_result("建 nexus 角色", _psql("postgres", role_sql))

    exists = _psql("postgres", "SELECT 1 FROM pg_database WHERE datname = 'nexus'")
    if exists.returncode == 0 and exists.stdout.strip() == "1":
        print("数据库 nexus 已存在，跳过创建。")
    else:
        _print_result("建 nexus 数据库", _psql("postgres", "CREATE DATABASE nexus OWNER nexus"))

    _print_result("创建 vector 扩展", _psql("nexus", "CREATE EXTENSION IF NOT EXISTS vector"))
    print("init-db 完成。连接串: postgresql+psycopg://nexus:<password>@localhost:5432/nexus")


def _is_running() -> bool:
    result = _run("pg_isready")
    return result.returncode == 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop, "status": status, "init-db": init_db, "psql": psql}[cmd]()
