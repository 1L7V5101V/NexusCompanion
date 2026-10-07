"""SQLAlchemy ORM models for the Akashic Agent database.

Import all models here so Alembic autogenerate discovers them
via Base.metadata.
"""

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin
from bootstrap.db.models.attachment import (
    ATTACHMENT_STATUSES,
    AttachmentModel,
    MessageAttachmentModel,
)
from bootstrap.db.models.extras import AppConfigModel, ScheduledJobModel
from bootstrap.db.models.memory import (
    ConsolidationEventModel,
    MemoryItemModel,
    MemoryReplacementModel,
)
from bootstrap.db.models.memory_engine import (
    ENGINE_ACTIONS,
    TenantMemoryEngineBindingModel,
    TenantMemoryEngineEventModel,
)
from bootstrap.db.models.persona import (
    PERSONA_ACTIONS,
    PERSONA_ACTORS,
    PERSONA_SOURCES,
    PersonaAuditEventModel,
    PersonaTemplateModel,
    TenantPersonaProfileModel,
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
from bootstrap.db.models.telegram import (
    BINDING_STATUSES,
    BINDING_VIAS,
    TelegramBindingCodeModel,
    TelegramIdentityBindingModel,
)
from bootstrap.db.models.tenant import TenantModel

__all__ = [
    "ATTACHMENT_STATUSES",
    "AppConfigModel",
    "AttachmentModel",
    "Base",
    "BINDING_STATUSES",
    "BINDING_VIAS",
    "ConsolidationEventModel",
    "ContextOnlyTimestampModel",
    "DeliveryModel",
    "ENGINE_ACTIONS",
    "MemoryItemModel",
    "MemoryReplacementModel",
    "MessageAttachmentModel",
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
    "TelegramBindingCodeModel",
    "TelegramIdentityBindingModel",
    "TenantMemoryEngineBindingModel",
    "TenantMemoryEngineEventModel",
    "TenantMixin",
    "TenantModel",
    "TickLogModel",
    "TickStepLogModel",
    "TimestampMixin",
]
