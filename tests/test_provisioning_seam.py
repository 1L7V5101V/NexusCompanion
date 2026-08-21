"""M4H-4 provisioning seam 最小契约测试（commit 1）。

状态机与哨兵语义是后续 worker / turn gate 的契约基础；具体行为
（幂等 DDL、并发去重、retry、rollback）由 commit 3-5 的实现测试覆盖。
"""
from infra.storage.provisioning import (
    PartitionNotReady,
    PartitionProvisioningFailed,
    PartitionStatus,
    TenantProvisioning,
)


def test_partition_status_states() -> None:
    assert PartitionStatus.UNKNOWN.value == "unknown"
    assert PartitionStatus.PENDING.value == "pending"
    assert PartitionStatus.READY.value == "ready"
    assert PartitionStatus.FAILED.value == "failed"
    assert {s.name for s in PartitionStatus} == {"UNKNOWN", "PENDING", "READY", "FAILED"}


def test_partition_not_ready_is_retryable() -> None:
    err = PartitionNotReady("tenant is pending")
    assert err.retryable is True
    assert err.error_type == "partition_not_ready"


def test_partition_provisioning_failed_not_retryable() -> None:
    err = PartitionProvisioningFailed("provisioning exhausted")
    assert err.retryable is False
    assert err.error_type == "partition_provisioning_failed"


def test_tenant_provisioning_is_runtime_checkable_protocol() -> None:
    import typing

    assert issubclass(TenantProvisioning, typing.Protocol)
    # 三个 seam 方法都定义在契约上，具体实现由 worker / gate 提供。
    for name in ("request_provisioning", "require_ready", "provision_tenant"):
        assert callable(getattr(TenantProvisioning, name))
