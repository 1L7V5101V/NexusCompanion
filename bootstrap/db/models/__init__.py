"""SQLAlchemy ORM models for the Akashic Agent database.

Import all models here so Alembic autogenerate discovers them
via Base.metadata.
"""

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin
from bootstrap.db.models.extras import AppConfigModel, ScheduledJobModel
from bootstrap.db.models.memory import (
    ConsolidationEventModel,
    MemoryItemModel,
    MemoryReplacementModel,
)
from bootstrap.db.models.proactive import (
    ContextOnlyTimestampModel,
    DeliveryModel,
    SessionStateModel,
    TickLogModel,
    TickStepLogModel,
)
from bootstrap.db.models.rachael import (
    RachaelActivationEventModel,
    RachaelEdgeModel,
    RachaelEmbeddingCacheModel,
    RachaelMigrationRunModel,
    RachaelNodeModel,
    RachaelQueryLogModel,
    RachaelSalienceStateModel,
    RachaelSourceSessionSnapshotModel,
)
from bootstrap.db.models.session import MessageModel, SessionModel
from bootstrap.db.models.tenant import TenantModel

__all__ = [
    "AppConfigModel",
    "Base",
    "ConsolidationEventModel",
    "ContextOnlyTimestampModel",
    "DeliveryModel",
    "MemoryItemModel",
    "MemoryReplacementModel",
    "MessageModel",
    "RachaelActivationEventModel",
    "RachaelEdgeModel",
    "RachaelEmbeddingCacheModel",
    "RachaelMigrationRunModel",
    "RachaelNodeModel",
    "RachaelQueryLogModel",
    "RachaelSalienceStateModel",
    "RachaelSourceSessionSnapshotModel",
    "ScheduledJobModel",
    "SessionModel",
    "SessionStateModel",
    "TenantMixin",
    "TenantModel",
    "TickLogModel",
    "TickStepLogModel",
    "TimestampMixin",
]
