#!/usr/bin/env python
"""PostgreSQL management helper for Akashic Agent.

Usage:
    python pg.py start       Start PostgreSQL
    python pg.py stop        Stop PostgreSQL
    python pg.py status      Check if PostgreSQL is running
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


def _is_running() -> bool:
    result = _run("pg_isready")
    return result.returncode == 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop, "status": status, "psql": psql}[cmd]()
