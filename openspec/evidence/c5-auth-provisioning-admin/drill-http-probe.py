"""C5 runbook 演练 HTTP 探针（在 canary 容器内对 127.0.0.1:2236 执行）。

只向 stdout 打印状态码与「已捕获」事实；明文 token / session cookie 仅写入指定
文件（容器内 /tmp/drill/），不进 stdout、不进 argv。供 c5_runbook_drill.sh 判定
路径 A/B/C 的预期结果（200 / 401）。

子命令：
  wait                                 等待服务端口就绪（重启后使用）
  admin-exchange <tokenfile> [ckfile]  POST /api/admin/auth/exchange
  user-exchange  <tokenfile>           POST /api/auth/exchange
  admin-get      <path> <ckfile>       GET 管理端点（带 admin cookie）
  create-account <ckfile> <name> <tokout>
                                       csrf → POST /api/admin/test-accounts，
                                       邀请明文写入 <tokout>
"""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:2236"
ORIGIN = "http://127.0.0.1:2237"  # ∈ canary config.toml [auth] origin_allowlist
ADMIN_COOKIE = "nexus_admin"  # cookie_secure=false 的退化名
USER_COOKIE = "nexus_session"


def _req(method: str, path: str, *, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8").strip()


def _cookie_from(headers, name: str) -> str:
    for header in headers.get_all("set-cookie") or []:
        for seg in header.split(";"):
            seg = seg.strip()
            if seg.startswith(name + "="):
                return seg.split("=", 1)[1]
    return ""


def cmd_wait(_args: list[str]) -> int:
    for _ in range(60):
        sock = socket.socket()
        sock.settimeout(2)
        try:
            sock.connect(("127.0.0.1", 2236))
            sock.close()
            print("service up")
            return 0
        except OSError:
            sock.close()
            time.sleep(2)
    print("service NOT up")
    return 1


def cmd_admin_exchange(args: list[str]) -> int:
    # exchange 是 mutation：ADR-4 要求 Origin/Referer 命中 allowlist，必须带头。
    status, _, headers = _req(
        "POST",
        "/api/admin/auth/exchange",
        body={"token": _read(args[0])},
        headers={"origin": ORIGIN},
    )
    print(f"admin-exchange status={status}")
    if status == 200 and len(args) > 1:
        raw = _cookie_from(headers, ADMIN_COOKIE)
        if raw:
            pathlib.Path(args[1]).write_text(raw, encoding="utf-8")
            print(f"admin cookie captured len={len(raw)}")
    return 0


def cmd_user_exchange(args: list[str]) -> int:
    # 同上：exchange 需带 Origin（ADR-4），否则 403。
    status, _, _ = _req(
        "POST",
        "/api/auth/exchange",
        body={"token": _read(args[0])},
        headers={"origin": ORIGIN},
    )
    print(f"user-exchange status={status}")
    return 0


def cmd_admin_get(args: list[str]) -> int:
    path, ckfile = args[0], args[1]
    status, _, _ = _req(
        "GET", path, headers={"cookie": f"{ADMIN_COOKIE}={_read(ckfile)}"}
    )
    print(f"admin-GET {path} status={status}")
    return 0


def cmd_create_account(args: list[str]) -> int:
    ckfile, name, tokout = args[0], args[1], args[2]
    headers = {"cookie": f"{ADMIN_COOKIE}={_read(ckfile)}"}
    status, body, _ = _req("GET", "/api/admin/auth/csrf", headers=headers)
    if status != 200:
        print(f"csrf status={status}")
        return 0
    csrf = json.loads(body)["csrf_token"]  # noqa: S105 - 短期 CSRF，非长期凭据
    status, body, _ = _req(
        "POST",
        "/api/admin/test-accounts",
        body={"display_name": name},
        headers={**headers, "origin": ORIGIN, "x-csrf-token": csrf},
    )
    print(f"create-account status={status}")
    if status == 200:
        data = json.loads(body)
        token = data.get("token", "")
        if token:
            pathlib.Path(tokout).write_text(token, encoding="utf-8")
            print(f"invitation token captured len={len(token)}")
        print(f"account status={data.get('account', {}).get('status')}")
    return 0


_COMMANDS = {
    "wait": cmd_wait,
    "admin-exchange": cmd_admin_exchange,
    "user-exchange": cmd_user_exchange,
    "admin-get": cmd_admin_get,
    "create-account": cmd_create_account,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        print(f"usage: {sys.argv[0]} <{'|'.join(_COMMANDS)}> [args...]")
        raise SystemExit(2)
    raise SystemExit(_COMMANDS[sys.argv[1]](sys.argv[2:]))
