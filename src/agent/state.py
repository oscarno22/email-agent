from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    NEWSLETTER = "newsletter"
    RECEIPT = "receipt"
    CALENDAR = "calendar"
    PERSONAL = "personal"
    WORK = "work"
    JUNK = "junk"
    UNKNOWN = "unknown"


class TrustPhase(StrEnum):
    SHADOW = "shadow"
    LABEL = "label"
    ARCHIVE = "archive"
    DRAFT = "draft"


class Email(BaseModel):
    gmail_id: str
    thread_id: str
    sender: str
    sender_domain: str
    subject: str
    body: str
    received_at: datetime


class Features(BaseModel):
    has_unsubscribe: bool = False
    has_links: bool = False
    body_excerpt: str = ""
    sender_known: bool = False


class Classification(BaseModel):
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    needs_escalation: bool = False


class ActionPlan(BaseModel):
    labels_to_apply: list[str] = Field(default_factory=list)
    archive: bool = False
    draft_reply: str | None = None
    notes: str = ""


class State(BaseModel):
    email: Email
    features: Features | None = None
    classification: Classification | None = None
    action: ActionPlan | None = None
    trust_phase: TrustPhase = TrustPhase.SHADOW
    log: list[str] = Field(default_factory=list)
