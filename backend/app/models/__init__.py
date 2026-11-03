"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic and ``create_all`` rely on.
"""

from app.models.agent import AgentRun, AgentRunStatus, ModelUsage
from app.models.analytics import AnalyticsSnapshot
from app.models.approval import ApprovalAction, ApprovalRecord
from app.models.audit import AuditLog
from app.models.brand import BrandProfile, CompetitorProfile, ContentPillar, MarketingGoal
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.creative import CreativeAsset, CreativeAssetStatus, CreativeKind
from app.models.idempotency import IdempotencyKey
from app.models.memory import MarketingMemory, MemoryKind
from app.models.social import (
    Platform,
    PublishedPost,
    ScheduledPost,
    SocialAccount,
    SocialAccountStatus,
)
from app.models.user import DEFAULT_ORGANIZATION_ID, User
from app.models.workflow import WorkflowCheckpoint, WorkflowWrite

__all__ = [
    "AgentRun",
    "AgentRunStatus",
    "ModelUsage",
    "AnalyticsSnapshot",
    "ApprovalAction",
    "ApprovalRecord",
    "AuditLog",
    "BrandProfile",
    "CompetitorProfile",
    "ContentPillar",
    "MarketingGoal",
    "Campaign",
    "CampaignStatus",
    "ContentItem",
    "ContentStatus",
    "ContentVersion",
    "CreativeAsset",
    "CreativeAssetStatus",
    "CreativeKind",
    "IdempotencyKey",
    "MarketingMemory",
    "MemoryKind",
    "Platform",
    "PublishedPost",
    "ScheduledPost",
    "SocialAccount",
    "SocialAccountStatus",
    "User",
    "DEFAULT_ORGANIZATION_ID",
    "WorkflowCheckpoint",
    "WorkflowWrite",
]
