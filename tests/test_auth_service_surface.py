"""C5 service ↔ API 接缝契约（纯单元，不需要 PG）。

存在理由：``bootstrap/auth/api.py`` 通过 ``runtime.<svc>.<method>`` 消费
``AuthService`` / ``AdminAuthService`` / ``ProvisioningService``。这条调用跨越
「3.x service 任务」与「4.x API 任务」两条任务边界——两侧各自的集成证据都只验证
自身那一侧，没人断言中间的方法契约。历史上 ``api.py`` 调用了
``runtime.provisioning.get_job``，而 ``ProvisioningService`` 并没有该方法（只有
``ProvisioningRepository`` 有）→ admin 建号未收敛时 AttributeError（500）。

本模块把这条接缝变成可执行断言，且**不依赖数据库**，因此没有 PG 也能跑。
放在 ``tests/`` 根而非 ``tests/auth_provisioning/``：后者 conftest 需要 PG 与
alembic（scratch DB 迁移），本模块刻意不引入这些依赖。
"""

from __future__ import annotations

import pathlib
import re

from bootstrap.auth import api as auth_api
from bootstrap.auth.service import (
    AdminAuthService,
    AuthService,
    ProvisioningService,
)

# api.py 中以 runtime.<svc>.<method> 形式触达 service 的调用点。
_RUNTIME_CALL = re.compile(r"runtime\.(auth|admin|provisioning)\.([A-Za-z_][A-Za-z0-9_]*)")

_SERVICE_CLASSES = {
    "auth": AuthService,
    "admin": AdminAuthService,
    "provisioning": ProvisioningService,
}


class _StubProvisioningRepo:
    """记录委派调用的假 repo（不碰数据库）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        async def _recorder(*args):
            self.calls.append((name, args))
            return {"stub": name, "args": args}

        return _recorder


class _StubExecutor:
    async def provision(self, *, account_id, tenant_id) -> None:
        raise AssertionError("接缝单测不应触发 executor")


def test_api_runtime_calls_exist_on_services() -> None:
    """api.py 里每个 ``runtime.<svc>.<method>`` 都必须存在于对应 service 上。

    这条断言覆盖 4.x（API）与 3.x（service）之间那条无人认领的接缝；
    ``runtime.provisioning.get_job`` 缺失正是靠它暴露，而不是等到线上 500。
    """
    source = pathlib.Path(auth_api.__file__).read_text(encoding="utf-8")
    referenced = set(_RUNTIME_CALL.findall(source))
    assert referenced, "未解析到任何 runtime.<svc>.<method> 调用：正则或源码结构已变"

    missing = sorted(
        f"{service}.{name}"
        for service, name in referenced
        if not hasattr(_SERVICE_CLASSES[service], name)
    )
    assert not missing, f"api.py 引用了 service 上不存在的方法: {missing}"


async def test_get_job_delegates_to_repository() -> None:
    """``ProvisioningService.get_job`` 必须存在并委派给 repo（失败可见性依赖它）。"""
    repo = _StubProvisioningRepo()
    service = ProvisioningService(repo, executor=_StubExecutor())

    job_id = "20000000-0000-0000-0000-0000000000c3"
    result = await service.get_job(job_id)

    assert result == {"stub": "get_job", "args": (job_id,)}
    assert repo.calls == [("get_job", (job_id,))]
