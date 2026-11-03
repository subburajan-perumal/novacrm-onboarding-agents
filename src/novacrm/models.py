"""Data contracts passed between the agents."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Tier(str, Enum):
    ENTERPRISE = "Enterprise"
    GROWTH = "Growth"


class RunState(str, Enum):
    RECEIVED = "RECEIVED"
    IGNORED = "IGNORED"                    # not a deal notification
    NEEDS_INFO = "NEEDS_INFO"              # asked the AE for missing fields
    VALIDATED = "VALIDATED"
    CALLING = "CALLING"
    TIER_CONFIRMED = "TIER_CONFIRMED"
    ESCALATED = "ESCALATED"                # handed to a human
    DUPLICATE_ESCALATED = "DUPLICATE_ESCALATED"
    PARKED_RL_DOWN = "PARKED_RL_DOWN"      # Rocketlane unavailable; resumable
    PROJECT_CREATED = "PROJECT_CREATED"
    CHANNEL_FAILED = "CHANNEL_FAILED"
    SUPERSEDED = "SUPERSEDED"              # AE replied with missing info; a new run took over
    DONE = "DONE"


class InboundEmail(BaseModel):
    message_id: str
    thread_id: str
    sender_email: str
    sender_name: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    received_at: str = ""
    rfc822_id: str = ""          # Message-ID header, for threaded replies


# ---- LLM extraction (structured output) ------------------------------------
class ExtractedField(BaseModel):
    value: Optional[str] = Field(None, description="The value exactly as written in the email, or null if absent.")
    evidence: Optional[str] = Field(None, description="Verbatim snippet from the email that contains the value, or null.")


class DealExtraction(BaseModel):
    is_deal_notification: bool = Field(description="True only if this email announces a closed/won deal to onboard.")
    customer_name: ExtractedField
    customer_contact_name: ExtractedField
    customer_contact_email: ExtractedField
    ae_name: ExtractedField
    salesforce_opportunity_url: ExtractedField
    close_date: ExtractedField
    notes: ExtractedField
    mentions_plan_tier: bool = Field(description="True if the email states Enterprise or Growth.")


class TranscriptJudgement(BaseModel):
    tier: Literal["Enterprise", "Growth", "Unclear"]
    ae_explicitly_confirmed: bool = Field(description="True only if the AE clearly said yes to the read-back of that tier.")
    reasoning: str


# ---- validated deal --------------------------------------------------------
class ParsedDeal(BaseModel):
    customer_name: str
    contact_name: Optional[str] = None
    contact_email: str
    ae_name: str
    ae_email: str
    ae_phone: str
    salesforce_url: str
    salesforce_opportunity_id: str
    close_date: Optional[str] = None
    notes: Optional[str] = None


class ValidationResult(BaseModel):
    ok: bool
    problems: list[str] = []


# ---- voice -----------------------------------------------------------------
CallStatus = Literal["answered", "no_answer", "busy", "voicemail", "failed"]


class CallOutcome(BaseModel):
    call_id: Optional[str] = None
    status: CallStatus
    tier: Optional[str] = None             # Retell post-call analysis: Enterprise / Growth / Unclear
    readback_confirmed: bool = False
    transcript: str = ""
    summary: str = ""
    raw_reason: str = ""


class TierDecision(BaseModel):
    action: Literal["accept", "retry", "escalate"]
    tier: Optional[Tier] = None
    reason: str


# ---- outputs ---------------------------------------------------------------
class ProjectResult(BaseModel):
    project_id: int
    project_name: str
    project_url: str
    template_id: int
    start_date: date
    due_date: date
    csm_name: str
    csm_email: str
    csm_model: str
    task_count: Optional[int] = None


class OnboardingHandoff(BaseModel):
    """What the Intake Agent hands to the Communication Agent."""
    run_id: str
    deal: ParsedDeal
    tier: Tier
    project: ProjectResult


class ChannelResult(BaseModel):
    channel_id: str
    channel_name: str
    topic: str
    welcome_text: str
    reused: bool = False
    internal_invites: list[str] = []
    customer_invite: str = "mocked"
