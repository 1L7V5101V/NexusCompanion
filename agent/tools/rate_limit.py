"""C7 task 3.3：租户级工具限流（web_search/web_fetch 的"经限流"基线）。

内存滑动窗口，按 (tenant_id, tool) 计数；单进程 Pilot 语义（重启即清零，
非 durable——限流是过载保护而非审计面）。仅对 user principal 生效（owner 不限）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Window:
    hits: deque[float] = field(default_factory=deque)


class TenantRateLimiter:
    """按 (tenant, 工具) 的滑动窗口限流。超限返回 False（调用方转结构化拒绝）。"""

    def __init__(self, *, max_calls: int = 30, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._buckets: dict[tuple[str, str], _Window] = {}

    def allow(self, tenant_id: str, tool: str, *, now: float | None = None) -> bool:
        if not tenant_id:
            return True  # 无租户归属（dev 兜底路径）不限
        ts = now if now is not None else time.monotonic()
        key = (tenant_id, tool)
        bucket = self._buckets.setdefault(key, _Window())
        cutoff = ts - self._window
        while bucket.hits and bucket.hits[0] < cutoff:
            bucket.hits.popleft()
        if len(bucket.hits) >= self._max_calls:
            return False
        bucket.hits.append(ts)
        return True

    def reset(self) -> None:
        self._buckets.clear()


_SHARED = TenantRateLimiter()


def shared_limiter() -> TenantRateLimiter:
    return _SHARED
