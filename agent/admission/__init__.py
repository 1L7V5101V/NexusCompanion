"""C3 tenant admission + bounded queue + restart recovery（task-03）。

本包承载 PILOT_ROADMAP §5.9.5 / §5.9.6 / §6.1 A·B 冻结的运行时契约：

- :mod:`agent.admission.queues` — 有界队列与 overload 语义（容量 = §10 DECIDED 初始值）；
- :mod:`agent.admission.lanes` — tenant-scoped serial lane（同 tenant 串行、跨 tenant 异步）；
- :mod:`agent.admission.resources` — LLM/embedding/MCP/process 分类资源 semaphore；
- :mod:`agent.admission.recovery` — 启动恢复扫描框架与 ``unknown``/``compensation_required`` 语义。

durable 表（inbox/turn/tool/work/outbox）归 C2；本包只交付 admission/queue/overload
与恢复扫描框架。丢失窗口记录见 ``agent/admission/LOST_WINDOWS.md``。
"""

from agent.admission.lanes import (
    LaneClosedError,
    TenantLaneRouter,
    WorkKind,
    resolve_admission_tenant,
)
from agent.admission.queues import (
    AdmissionLimits,
    AdmissionOverloadError,
    BoundedAdmissionQueue,
)
from agent.admission.recovery import (
    PendingWorkRecord,
    RecoveryAction,
    RecoveryRecord,
    StartupRecoveryScanner,
    ToolOutcomeStatus,
)
from agent.admission.resources import (
    ResourceKind,
    ResourceLimits,
    ResourceSemaphores,
)

__all__ = [
    "AdmissionLimits",
    "AdmissionOverloadError",
    "BoundedAdmissionQueue",
    "LaneClosedError",
    "PendingWorkRecord",
    "RecoveryAction",
    "RecoveryRecord",
    "ResourceKind",
    "ResourceLimits",
    "ResourceSemaphores",
    "StartupRecoveryScanner",
    "TenantLaneRouter",
    "ToolOutcomeStatus",
    "WorkKind",
    "resolve_admission_tenant",
]
