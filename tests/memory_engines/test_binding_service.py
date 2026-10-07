"""C14 binding 服务/仓储验收（task 1.2/1.3）。

单 active 约束 + 初始绑定 + revision 提升 + 切换校验（目录/opt-in/readiness）+
数据隔离可追踪（事件表）+ 目录冻结（inspector 不入目录）+ 快照缓存。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bootstrap.memory_binding import (
    DEFAULT_MEMORY_ENGINE_ID,
    MEMORY_ENGINE_CATALOG,
    EngineNotReadyError,
    EngineNotSelectableError,
    TenantMemoryEngineBindingService,
    UnknownEngineError,
)

pytestmark = pytest.mark.postgres


def _service(factory: Any, *, ready: tuple[str, ...] = ("default", "rachael"), user_selection: bool = True) -> TenantMemoryEngineBindingService:
    return TenantMemoryEngineBindingService(
        factory, ready_engines=ready, user_selection_allowed=user_selection
    )


def test_catalog_is_frozen_server_side() -> None:
    """验收 1/8：目录为服务端冻结常量，仅 default/rachael，inspector 不入目录。"""
    assert {e.engine_id for e in MEMORY_ENGINE_CATALOG} == {"default", "rachael"}
    assert all("inspector" not in e.engine_id for e in MEMORY_ENGINE_CATALOG)
    policies = {e.engine_id: e.binding_policy for e in MEMORY_ENGINE_CATALOG}
    assert policies == {"default": "default_on", "rachael": "opt_in"}


async def test_ensure_binding_initial_default_and_idempotent(me_factory: Any) -> None:
    """验收 6：初始绑定为 default；重复 ensure 幂等（不提升 revision/不重复事件）。"""
    svc = _service(me_factory)
    first = await svc.ensure_binding("t1")
    assert first["engine_id"] == DEFAULT_MEMORY_ENGINE_ID
    assert first["tenant_policy_revision"] == 0
    again = await svc.ensure_binding("t1")
    assert again["engine_id"] == DEFAULT_MEMORY_ENGINE_ID
    assert again["tenant_policy_revision"] == 0
    events = await svc.list_events("t1")
    assert [e["action"] for e in events] == ["initial"]


async def test_single_active_constraint_and_concurrent_ensure(
    me_factory: Any, exec_sql: Any
) -> None:
    """验收 6：单 active 约束（PK）+ 并发 ensure 只产生一行。"""

    async def _ensure() -> None:
        svc = _service(me_factory)
        await svc.ensure_binding("t_race")

    await asyncio.gather(_ensure(), _ensure(), _ensure())
    rows = exec_sql(
        "SELECT tenant_id, engine_id, tenant_policy_revision FROM "
        "tenant_memory_engine_bindings WHERE tenant_id = 't_race'"
    )
    assert len(rows) == 1
    assert rows[0][1] == "default" and rows[0][2] == 0
    events = exec_sql(
        "SELECT action FROM tenant_memory_engine_events WHERE tenant_id = 't_race'"
    )
    assert [e[0] for e in events] == ["initial"]


async def test_switch_persists_and_bumps_revision(me_factory: Any, exec_sql: Any) -> None:
    """验收 3：切换持久化 PG + revision 恰好 +1 + switch 事件；重启（重读）仍生效。"""
    svc = _service(me_factory)
    await svc.ensure_binding("t2")
    record = await svc.switch("t2", "rachael", actor="user:abc")
    assert record["engine_id"] == "rachael"
    assert record["tenant_policy_revision"] == 1
    # 模拟重启：全新服务实例读 PG。
    fresh = _service(me_factory)
    assert await fresh.resolve_active_engine("t2") == "rachael"
    assert await fresh.resolve_revision("t2") == 1
    events = await svc.list_events("t2")
    assert [(e["action"], e["engine_id"], e["tenant_policy_revision"]) for e in events] == [
        ("switch", "rachael", 1),
        ("initial", "default", 0),
    ]
    # 事件表按 (tenant_id, engine_id) 可追踪（验收 5）。
    rows = exec_sql(
        "SELECT tenant_id, engine_id, action FROM tenant_memory_engine_events "
        "WHERE tenant_id = 't2' ORDER BY id"
    )
    assert rows == [("t2", "default", "initial"), ("t2", "rachael", "switch")]


async def test_switch_same_engine_is_idempotent(me_factory: Any) -> None:
    """验收 2：同 engine 重复提交幂等——不提升 revision、不写切换事件。"""
    svc = _service(me_factory)
    await svc.ensure_binding("t3")
    first = await svc.switch("t3", "default")
    assert first["tenant_policy_revision"] == 0
    events = await svc.list_events("t3")
    assert [e["action"] for e in events] == ["initial"]


async def test_unauthorized_switch_rejected(me_factory: Any, exec_sql: Any) -> None:
    """验收 2：目录外 404 / not ready 409 / 用户侧切换关闭 403——均无持久化副作用。"""
    # 目录外。
    full = _service(me_factory)
    with pytest.raises(UnknownEngineError):
        await full.switch("t4", "inspector")
    with pytest.raises(UnknownEngineError):
        await full.switch("t4", "ghost-engine")
    # not ready：rachael 未构建。
    default_only = _service(me_factory, ready=("default",))
    await default_only.ensure_binding("t4")
    with pytest.raises(EngineNotReadyError):
        await default_only.switch("t4", "rachael")
    # 用户侧切换关闭（管理员未放行）。
    locked = _service(me_factory, user_selection=False)
    await locked.ensure_binding("t4")
    with pytest.raises(EngineNotSelectableError):
        await locked.switch("t4", "rachael")
    # 全部失败后：binding/revision/事件保持初始态。
    rows = exec_sql(
        "SELECT engine_id, tenant_policy_revision FROM "
        "tenant_memory_engine_bindings WHERE tenant_id = 't4'"
    )
    assert rows == [("default", 0)]
    events = exec_sql(
        "SELECT action FROM tenant_memory_engine_events WHERE tenant_id = 't4'"
    )
    assert [e[0] for e in events] == ["initial"]


async def test_switch_does_not_touch_engine_data_traceability(
    me_factory: Any, exec_sql: Any
) -> None:
    """验收 5：切换只写 binding/事件（不迁移/不删除引擎数据）；历史可追踪。"""
    svc = _service(me_factory)
    await svc.ensure_binding("t5")
    await svc.switch("t5", "rachael")
    await svc.switch("t5", "default")
    await svc.switch("t5", "rachael")
    rows = exec_sql(
        "SELECT engine_id, tenant_policy_revision FROM "
        "tenant_memory_engine_bindings WHERE tenant_id = 't5'"
    )
    assert rows == [("rachael", 3)]
    events = exec_sql(
        "SELECT engine_id, action, tenant_policy_revision FROM "
        "tenant_memory_engine_events WHERE tenant_id = 't5' ORDER BY id"
    )
    assert events == [
        ("default", "initial", 0),
        ("rachael", "switch", 1),
        ("default", "switch", 2),
        ("rachael", "switch", 3),
    ]
    # 仓储面不存在任何引擎数据操作入口（结构保证：repo 只触碰两表）。
    from bootstrap.db.repository import memory_engine_repo

    public_api = {
        name for name in dir(memory_engine_repo.MemoryEngineBindingRepository)
        if not name.startswith("_")
    }
    assert public_api == {
        "create_initial_binding",
        "get_binding",
        "list_bindings",
        "list_events",
        "switch_binding",
    }


async def test_concurrent_switch_keeps_single_row(me_factory: Any, exec_sql: Any) -> None:
    """验收 6：并发切换不破坏单 active（终态单行 + revision 反映全部成功切换）。"""

    async def _switch(engine: str) -> None:
        svc = _service(me_factory)
        await svc.ensure_binding("t6")
        await svc.switch("t6", engine)

    await asyncio.gather(_switch("rachael"), _switch("default"))
    rows = exec_sql(
        "SELECT engine_id, tenant_policy_revision FROM "
        "tenant_memory_engine_bindings WHERE tenant_id = 't6'"
    )
    # 单 active 约束：始终恰一行；revision 与 switch 事件数一致（原子 +1 不丢失，
    # 服务层幂等读可能合法跳过与当前态相同的目标 → revision ∈ {1, 2}）。
    assert len(rows) == 1
    revision = rows[0][1]
    assert revision in {1, 2}
    switch_events = exec_sql(
        "SELECT count(*) FROM tenant_memory_engine_events "
        "WHERE tenant_id = 't6' AND action = 'switch'"
    )
    assert int(switch_events[0][0]) == revision
    assert rows[0][0] in {"default", "rachael"}


async def test_snapshot_cache_for_ingest_gate(me_factory: Any) -> None:
    """ingest 门控快照：resolve 刷新缓存；未命中 None（fail-open 由门控决定）。"""
    svc = _service(me_factory)
    assert svc.snapshot_active("t7") is None
    await svc.resolve_active_engine("t7")
    assert svc.snapshot_active("t7") == "default"
    await svc.switch("t7", "rachael")
    assert svc.snapshot_active("t7") == "rachael"


async def test_describe_view(me_factory: Any) -> None:
    """GET 视图：目录 + ready/selectable/active/revision（inspector 不出现）。"""
    svc = _service(me_factory, ready=("default",))
    view = await svc.describe("t8")
    assert [e["engine_id"] for e in view["engines"]] == ["default", "rachael"]
    by_id = {e["engine_id"]: e for e in view["engines"]}
    assert by_id["default"]["ready"] is True
    assert by_id["default"]["selectable"] is True
    assert by_id["default"]["active"] is True
    assert by_id["rachael"]["ready"] is False
    assert by_id["rachael"]["selectable"] is False
    assert view["active_engine"] == "default"
    assert view["tenant_policy_revision"] == 0
