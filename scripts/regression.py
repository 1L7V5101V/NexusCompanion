"""可复现回归入口：PG 前置 → 全量回归 → 证据落盘。

背景
----
canonical identity / control plane / migration / storage 集成测试在本地 PostgreSQL
不可用时整组 ``skip``（见 ``tests/conftest.py::pytest_configure`` 与各 ``conftest.py``
的 session fixture）。于是一次「看起来全绿」的回归实际可能漏掉上百条
durability / turn / outbox / identity 断言——2026-09-20 的 main 全量回归即
``166 skipped``，其中 117 项是 ``postgres`` marker。

本脚本把「起库 → 校验 → 全量回归 → 落证据」固化成一个可复现命令，避免每次手工
拼 pytest 参数，也避免把 skip 误读成通过。

用法
----
    python scripts/regression.py                       # 校验 PG 可达后跑全量回归
    python scripts/regression.py --start-pg             # 先用 docker compose 起 pgvector 库
    python scripts/regression.py --evidence <path>      # 原始输出同时写入证据文件
    python scripts/regression.py -- tests/admission     # ``--`` 之后透传给 pytest

退出码
------
pytest 的退出码原样返回；PG 前置失败返回 2。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker" / "debug" / "docker-compose.yml"
DEFAULT_PG_URL = "postgresql://nexus:nexus_dev@localhost:5433/nexus"


def pg_url() -> str:
    """集成测试使用的 PG 连接串（与各 conftest 的 ``NEXUS_TEST_PG_URL`` 同源）。"""
    return os.environ.get("NEXUS_TEST_PG_URL", DEFAULT_PG_URL)


def pg_reachable(url: str | None = None) -> bool:
    """PG 是否可达（2s 超时，不抛）。"""
    import psycopg

    try:
        conn = psycopg.connect(url or pg_url(), connect_timeout=2)
    except Exception:
        return False
    conn.close()
    return True


def start_pg(timeout: float = 90.0) -> None:
    """用 ``docker/debug`` compose 起 pgvector 开发库并等待就绪。"""
    if not COMPOSE_FILE.exists():
        raise SystemExit(f"[regression] 缺少 compose 文件：{COMPOSE_FILE}")
    print(f"[regression] 启动 PG：docker compose -f {COMPOSE_FILE} up -d postgres")
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "postgres"],
        cwd=REPO_ROOT,
        check=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pg_reachable():
            print("[regression] PG 已就绪")
            return
        time.sleep(2.0)
    raise SystemExit(f"[regression] PG 在 {timeout:.0f}s 内未就绪：{pg_url()}")


def _run_pytest(cmd: list[str], env: dict[str, str], evidence: Path | None) -> int:
    """执行 pytest；``evidence`` 非空时边跑边把原始输出写入该文件。"""
    if evidence is None:
        return subprocess.run(cmd, cwd=REPO_ROOT, env=env).returncode

    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("w", encoding="utf-8", newline="\n") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        returncode = proc.wait()
    print(f"[regression] 证据写入 {evidence}")
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="可复现回归入口（PG 前置 + 全量 pytest）",
        epilog="PG 不可达时直接失败，因为集成测试会 skip，结果不能作为回归证据。",
    )
    parser.add_argument(
        "--start-pg",
        action="store_true",
        help="先用 docker/debug compose 启动 pgvector 开发库（需本机有 docker）",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
        help="把原始 pytest 输出写入该文件（如 openspec/evidence/<change>/pytest-regression.txt）",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="透传给 pytest 的目标/参数（放在 `--` 之后）；留空则跑 `tests/`",
    )
    args = parser.parse_args(argv)

    if args.start_pg:
        start_pg()

    if not pg_reachable():
        print(
            "[regression] PostgreSQL 不可达：" + pg_url() + "\n"
            "  起库：docker compose -f docker/debug/docker-compose.yml up -d postgres\n"
            "  改址：设置 NEXUS_TEST_PG_URL 指向可用的 pgvector 实例\n"
            "  说明：无 PG 时集成测试整组 skip，结果不可作为回归证据，故此处直接失败。",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env["NEXUS_REQUIRE_PG"] = "1"  # 双保险：conftest 守卫同样会在 PG 丢失时中止

    cmd = [sys.executable, "-m", "pytest", "-q", "-W", "error", *args.pytest_args]
    if not args.pytest_args:
        cmd.append("tests/")
    print(f"[regression] {' '.join(cmd)}")
    return _run_pytest(cmd, env, args.evidence)


if __name__ == "__main__":
    raise SystemExit(main())
