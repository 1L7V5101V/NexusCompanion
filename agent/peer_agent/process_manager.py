"""
PeerProcessManager：管理 peer agent 子进程的完整生命周期。

职责：
- 冷启动：任务提交前检查健康，未运行则自动拉起子进程
- 终止：任务完成后由 Poller 调用，销毁子进程
- 全局关闭：Akasic 退出时批量终止所有子进程
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.net.http import HttpRequester, RequestBudget

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = 2.0
_SPAWN_POLL_INTERVAL_S = 1.0


@dataclass
class PeerProcessConfig:
    name: str
    base_url: str
    launcher: list[str]          # 拉起命令，如 ["uv", "run", "python", "-m", "app.a2a_server"]
    cwd: str | None = None       # 子进程工作目录，None 表示继承父进程
    health_path: str = "/health"
    startup_timeout_s: int = 30
    shutdown_timeout_s: int = 10
    log_dir: str = "runtime/peer_agents"


class PeerProcessManager:
    """管理 peer agent 子进程的生命周期。"""

    def __init__(
        self,
        configs: list[PeerProcessConfig],
        requester: HttpRequester,
    ) -> None:
        self._configs: dict[str, PeerProcessConfig] = {c.name: c for c in configs}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._requester = requester
        self._locks: dict[str, asyncio.Lock] = {c.name: asyncio.Lock() for c in configs}

    async def ensure_ready(self, name: str) -> None:
        """确保指定 agent 已启动且通过健康检查。未启动则冷启动。"""
        cfg = self._configs.get(name)
        if cfg is None:
            raise ValueError(f"未知 peer agent: {name!r}")

        async with self._locks[name]:
            if await self._is_healthy(cfg):
                logger.debug("[PeerProcess] %s 已在线", name)
                return
            logger.info("[PeerProcess] %s 未运行，开始冷启动", name)
            await self._spawn(cfg)
            logger.info("[PeerProcess] %s 启动成功", name)

    async def terminate(self, name: str) -> None:
        """销毁指定 peer agent 进程（任务完成后调用）。"""
        async with self._locks[name]:
            proc = self._procs.pop(name, None)
            if proc is None:
                return
            logger.info("[PeerProcess] 终止 %s (pid=%s)", name, proc.pid)
            await self._kill(proc, self._configs[name].shutdown_timeout_s)

    async def shutdown_all(self) -> None:
        """Akasic 退出时批量终止所有子进程。"""
        names = list(self._procs.keys())
        if names:
            logger.info("[PeerProcess] 关闭所有子进程: %s", names)
        await asyncio.gather(
            *(self.terminate(name) for name in names),
            return_exceptions=True,
        )

    # ── 内部方法 ──────────────────────────────────────────────

    async def _is_healthy(self, cfg: PeerProcessConfig) -> bool:
        try:
            r = await self._requester.get(
                cfg.base_url.rstrip("/") + cfg.health_path,
                budget=RequestBudget(total_timeout_s=_HEALTH_TIMEOUT_S),
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _spawn(self, cfg: PeerProcessConfig) -> None:
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{cfg.name.replace(' ', '_')}.log"

        log_fp = log_path.open("ab")
        proc = await asyncio.create_subprocess_exec(
            *cfg.launcher,
            stdout=log_fp,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cfg.cwd,
        )
        self._procs[cfg.name] = proc
        logger.info("[PeerProcess] 已启动 %s pid=%d 日志=%s", cfg.name, proc.pid, log_path)

        # 等待健康检查通过
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.startup_timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(_SPAWN_POLL_INTERVAL_S)
            if proc.returncode is not None:
                raise RuntimeError(f"{cfg.name} 启动后立即退出 (rc={proc.returncode})")
            if await self._is_healthy(cfg):
                return

        proc.terminate()
        self._procs.pop(cfg.name, None)
        raise RuntimeError(f"{cfg.name} 启动超时（{cfg.startup_timeout_s}s）")

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process, timeout_s: int) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(timeout_s))
        except asyncio.TimeoutError:
            logger.warning("[PeerProcess] SIGTERM 超时，强制 SIGKILL pid=%d", proc.pid)
            proc.kill()
            await proc.wait()
