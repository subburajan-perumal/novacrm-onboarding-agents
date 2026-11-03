"""Deterministic guardrails. The LLM proposes; these rules decide.

1. Required-field validation with an evidence check (no guessed values).
2. Tier acceptance: only a clear, read-back-confirmed tier from a connected call, agreed by an
   independent transcript check. Otherwise retry, then escalate to a human. Never default a tier.
3. Welcome-message check: personalized facts present, no other-tier terms.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import Config
from .models import (
    CallOutcome,
    DealExtraction,
    InboundEmail,
    ParsedDeal,
    Tier,
    TierDecision,
    TranscriptJudgement,
    ValidationResult,
)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SF_URL_RE = re.compile(
    r"^https://[A-Za-z0-9.-]+\.(?:lightning\.force\.com|my\.salesforce\.com|salesforce\.com)/\S*?"
    r"(?P<opp>006[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?)\b"
)
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def grounded(value: Optional[str], source_text: str) -> bool:
    """A value is grounded only if it literally appears in the email (whitespace/case-insensitive)."""
    if not value:
        return False
    return _norm(value) in _norm(source_text)


def validate_deal(
    extraction: DealExtraction, email: InboundEmail, source_text: str, cfg: Config
) -> tuple[Optional[ParsedDeal], ValidationResult]:
    """Return a ParsedDeal only when every required field is present, grounded and well-formed."""
    problems: list[str] = []

    def take(field: str, label: str) -> Optional[str]:
        f = getattr(extraction, field)
        if not f.value or not f.value.strip():
            problems.append(f"{label} is missing")
            return None
        if not grounded(f.value, source_text):
            problems.append(f"{label} could not be found verbatim in the email (not guessing)")
            return None
        return f.value.strip()

    customer = take("customer_name", "Customer name")
    contact_email = take("customer_contact_email", "Customer contact email")
    sf_url = take("salesforce_opportunity_url", "Salesforce opportunity link")

    if contact_email and not EMAIL_RE.match(contact_email):
        problems.append(f"Customer contact email '{contact_email}' is not a valid email address")
        contact_email = None
    if contact_email:
        domain = contact_email.split("@")[-1].lower()
        if domain == cfg.settings["company_domain"] or contact_email.lower() == email.sender_email.lower():
            problems.append("Customer contact email points to NovaCRM/the AE, not the customer")
            contact_email = None

    opp_id = None
    if sf_url:
        m = SF_URL_RE.match(sf_url)
        if not m:
            problems.append(f"Salesforce link '{sf_url}' is not a Salesforce Opportunity URL (006... id)")
        else:
            opp_id = m.group("opp")

    ae = cfg.find_ae(email.sender_email)
    if ae is None:
        problems.append(f"Sender {email.sender_email} is not a registered Account Executive (no phone on file)")
    elif not ae.phone or not ae.phone.startswith("+"):
        problems.append(f"No valid phone number on file for AE {ae.name}")

    contact_name = extraction.customer_contact_name.value
    if contact_name and not grounded(contact_name, source_text):
        contact_name = None  # optional field: drop ungrounded values silently

    if problems:
        return None, ValidationResult(ok=False, problems=problems)

    ae_name = extraction.ae_name.value if grounded(extraction.ae_name.value, source_text) else ae.name
    deal = ParsedDeal(
        customer_name=customer,
        contact_name=contact_name,
        contact_email=contact_email,
        ae_name=ae_name or ae.name,
        ae_email=ae.email,
        ae_phone=ae.phone,
        salesforce_url=sf_url,
        salesforce_opportunity_id=opp_id,
        close_date=extraction.close_date.value if grounded(extraction.close_date.value, source_text) else None,
        notes=extraction.notes.value if grounded(extraction.notes.value, source_text) else None,
    )
    return deal, ValidationResult(ok=True)


# ---- tier confirmation -----------------------------------------------------
UNREACHABLE = {"no_answer", "busy", "voicemail", "failed"}


def decide_tier(
    outcome: CallOutcome,
    judgement: Optional[TranscriptJudgement],
    attempt: int,
    unclear_attempts: int,
    cfg: Config,
) -> TierDecision:
    """attempt / unclear_attempts are counts *including* this call."""
    max_attempts = cfg.voice["max_attempts"]
    max_unclear = cfg.voice["max_unclear_attempts"]

    if outcome.status in UNREACHABLE:
        if attempt < max_attempts:
            return TierDecision(action="retry", reason=f"AE not reached ({outcome.status}); attempt {attempt}/{max_attempts}")
        return TierDecision(action="escalate", reason=f"AE not reached after {attempt} attempts (last: {outcome.status})")

    voice_tier = outcome.tier if outcome.tier in (Tier.ENTERPRISE.value, Tier.GROWTH.value) else None
    judge_tier = judgement.tier if judgement and judgement.tier != "Unclear" else None
    clear = (
        voice_tier is not None
        and outcome.readback_confirmed
        and judgement is not None
        and judge_tier == voice_tier
        and judgement.ae_explicitly_confirmed
    )
    if clear:
        return TierDecision(action="accept", tier=Tier(voice_tier),
                            reason=f"AE confirmed {voice_tier} on read-back; transcript check agrees")

    why = []
    if voice_tier is None:
        why.append(f"voice agent captured '{outcome.tier or 'nothing'}'")
    if not outcome.readback_confirmed:
        why.append("no explicit yes to the read-back")
    if judgement is None:
        why.append("transcript check unavailable")
    elif judge_tier != voice_tier:
        why.append(f"transcript check says '{judgement.tier}'")
    elif not judgement.ae_explicitly_confirmed:
        why.append("transcript check found no explicit confirmation")
    reason = "Ambiguous tier: " + "; ".join(why)
    if unclear_attempts < max_unclear and attempt < max_attempts:
        return TierDecision(action="retry", reason=reason + f" (re-calling, unclear {unclear_attempts}/{max_unclear})")
    return TierDecision(action="escalate", reason=reason + " (retries exhausted)")


# ---- welcome message -------------------------------------------------------
def check_welcome(text: str, *, customer: str, tier: Tier, duration_days: int, csm_name: str) -> list[str]:
    problems = []
    low = text.lower()
    if customer.lower() not in low:
        problems.append("customer name missing")
    if str(duration_days) not in text:
        problems.append(f"{duration_days}-day timeline missing")
    if csm_name.lower() not in low:
        problems.append("CSM name missing")
    other = Tier.GROWTH if tier == Tier.ENTERPRISE else Tier.ENTERPRISE
    other_days = 14 if tier == Tier.ENTERPRISE else 30
    if other.value.lower() in low:
        problems.append(f"mentions the {other.value} tier")
    if re.search(rf"\b{other_days}[- ]day", low):
        problems.append(f"mentions a {other_days}-day timeline")
    if tier == Tier.GROWTH and "dedicated" in low:
        problems.append("promises a dedicated CSM to a Growth customer")
    return problems
