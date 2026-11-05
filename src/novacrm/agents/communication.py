"""Agent 2 - Communication.

Creates the onboarding Slack channel personalized by plan tier: channel name, topic, purpose and a
welcome message (Claude draft, checked by guardrails; static template as the fallback).
"""
from __future__ import annotations

import re

from ..adapters import ChatClient, LLMClient
from ..audit import AuditLog
from ..config import Config
from ..guardrails import check_welcome
from ..models import ChannelResult, OnboardingHandoff, Tier

AGENT = "communication"


def channel_name(customer: str, suffix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", customer.lower()).strip("-")[:60].strip("-")
    return f"onb-{slug}-{suffix}"


def static_welcome(h: OnboardingHandoff, days: int) -> str:
    p, d = h.project, h.deal
    who = (f"your dedicated CSM, *{p.csm_name}*" if h.tier == Tier.ENTERPRISE
           else f"*{p.csm_name}*, our Growth CSM team")
    contact = f" {d.contact_name.split()[0]}" if d.contact_name else ""
    return (
        f":wave: Welcome{contact}, and welcome *{d.customer_name}* to NovaCRM!\n\n"
        f"This is our shared channel for your *{h.tier.value}* onboarding: a *{days}-day* plan "
        f"(target go-live {p.due_date:%d %b %Y}). You'll work with {who}.\n\n"
        "We'll move through four phases:\n• Kickoff\n• Data Migration (with your sign-off on migrated data)\n"
        "• Configuration\n• Go-Live\n\n"
        f"*Next step:* let's schedule the kickoff call. {p.csm_name} will share time slots here shortly."
    )


class CommunicationAgent:
    def __init__(self, cfg: Config, chat: ChatClient, llm: LLMClient, audit: AuditLog):
        self.cfg, self.chat, self.llm, self.audit = cfg, chat, llm, audit

    def run(self, h: OnboardingHandoff) -> ChannelResult:
        tcfg = self.cfg.tier(h.tier.value)
        days = tcfg["duration_days"]
        name = channel_name(h.deal.customer_name, tcfg["channel_suffix"])

        existing = self.chat.find_channel(name)
        channel_id = existing or self.chat.create_channel(name)
        self.audit.log(h.run_id, AGENT, "channel_ready", input={"name": name},
                       output={"channel_id": channel_id, "reused": bool(existing)},
                       decision="REUSED" if existing else "CREATED",
                       rationale="Channel already existed; reused (idempotent)" if existing
                       else f"New {h.tier.value} channel for {h.deal.customer_name}")

        topic = (f"{h.tier.value} onboarding · {days}-day plan · {h.project.csm_model} CSM: {h.project.csm_name} · "
                 f"Go-live target {h.project.due_date:%d %b %Y}")
        purpose = f"Shared onboarding channel for {h.deal.customer_name} and NovaCRM. Rocketlane: {h.project.project_url}"
        self.chat.set_topic(channel_id, topic)
        self.chat.set_purpose(channel_id, purpose)

        facts = {
            "customer company": h.deal.customer_name,
            "customer contact first name": (h.deal.contact_name or "").split()[0] if h.deal.contact_name else "(unknown)",
            "plan tier": h.tier.value,
            "onboarding length": f"{days} days",
            "target go-live": f"{h.project.due_date:%d %b %Y}",
            "CSM": f"{h.project.csm_name} ({'dedicated CSM' if h.tier == Tier.ENTERPRISE else 'pooled Growth CSM team'})",
            "phases": "Kickoff, Data Migration (customer signs off migrated data), Configuration, Go-Live",
            "next step": "schedule the kickoff call; the CSM will share time slots in this channel",
        }
        source, problems = "claude", []
        try:
            welcome = self.llm.draft_welcome(facts)
            problems = check_welcome(welcome, customer=h.deal.customer_name, tier=h.tier,
                                     duration_days=days, csm_name=h.project.csm_name)
        except Exception as exc:
            welcome, problems = "", [f"draft failed: {type(exc).__name__}"]
        if problems:
            source = "static_template"
            welcome = static_welcome(h, days)
        self.audit.log(h.run_id, "guardrail", "welcome_message_check", output={"problems": problems, "source": source},
                       decision="PASS" if source == "claude" else "FALLBACK",
                       rationale="Draft must name the customer, tier timeline and CSM, and nothing from the other tier")

        self.chat.post_message(channel_id, welcome)
        self.chat.post_message(channel_id, f":clipboard: Onboarding plan in Rocketlane: {h.project.project_url}")

        invited, missing = [], []
        aliases = self.cfg.settings.get("slack", {}).get("email_aliases") or {}
        for email in dict.fromkeys((h.project.csm_email, h.deal.ae_email)):
            uid = self.chat.lookup_user(aliases.get(email, email))
            if uid and uid not in invited:
                invited.append(uid)
            elif not uid:
                missing.append(email)
        self.chat.invite(channel_id, invited)
        self.audit.log(h.run_id, AGENT, "internal_invites", output={"invited": invited, "not_in_slack": missing},
                       decision="INVITED", rationale="CSM and AE join the customer channel")

        self.audit.log(h.run_id, AGENT, "customer_invite_mocked",
                       input={"emails": [h.deal.contact_email]},
                       output={"would_call": "conversations.inviteShared"}, decision="MOCKED",
                       rationale="Slack Connect (external customer invite) needs a paid Slack plan; production calls inviteShared")
        result = ChannelResult(channel_id=channel_id, channel_name=name, topic=topic, welcome_text=welcome,
                               reused=bool(existing), internal_invites=invited)
        self.audit.log(h.run_id, AGENT, "channel_personalized", output=result, decision="DONE",
                       rationale=f"Channel name, topic and welcome message set for the {h.tier.value} plan")
        return result
