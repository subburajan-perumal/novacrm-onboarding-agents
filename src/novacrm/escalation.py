"""Human-in-the-loop: escalations to the CS manager and clarification requests to the AE."""
from __future__ import annotations

from typing import Optional

from .adapters import ChatClient, MailClient
from .audit import AuditLog
from .config import Config
from .models import InboundEmail


class Escalator:
    def __init__(self, cfg: Config, mail: MailClient, audit: AuditLog, chat: Optional[ChatClient] = None):
        self.cfg = cfg
        self.mail = mail
        self.audit = audit
        self.chat = chat

    def escalate(self, run_id: str, email: InboundEmail, reason: str, details: str = "", customer: str = "") -> None:
        mgr = self.cfg.settings["cs_manager"]
        subject = f"[Onboarding needs a human] {customer or email.subject} - {reason[:80]}"
        body = (
            f"Hi {mgr['name'].split()[0]},\n\n"
            f"The onboarding agent stopped and needs a human decision.\n\n"
            f"Run: {run_id}\nDeal email: \"{email.subject}\" from {email.sender_email}\n"
            f"Reason: {reason}\n\n{details}\n\n"
            "Nothing was assumed: no Rocketlane project or Slack channel is created until this is resolved.\n"
            "Resolve it, then re-run the deal or reply on the AE thread.\n\n- NovaCRM Onboarding Agent"
        )
        self.mail.send(mgr["email"], subject, body)
        self.mail.add_labels(email.message_id, [self.cfg.gmail["labels"]["escalated"]])
        posted = False
        if self.chat is not None:
            try:
                ch = self.chat.find_channel(self.cfg.settings["slack"]["escalation_channel"])
                if ch:
                    self.chat.post_message(ch, f":rotating_light: *Onboarding needs a human* ({run_id})\n*{customer or email.subject}*: {reason}")
                    posted = True
            except Exception as exc:  # escalation must never crash on the secondary channel
                self.audit.log(run_id, "orchestrator", "escalation_slack_failed", output=str(exc))
        self.audit.log(run_id, "orchestrator", "escalated_to_human", input={"reason": reason},
                       output={"emailed": mgr["email"], "slack_posted": posted},
                       decision="ESCALATE", rationale=reason)

    def ask_ae_for_info(self, run_id: str, email: InboundEmail, problems: list[str]) -> None:
        bullets = "\n".join(f"  - {p}" for p in problems)
        body = (
            f"Hi {email.sender_name.split()[0].title() if email.sender_name else 'there'},\n\n"
            "Thanks for the deal notification. I couldn't start onboarding yet because:\n"
            f"{bullets}\n\n"
            "Please reply on this thread with the missing details (customer name, customer contact email "
            "and the Salesforce opportunity link). I won't guess any of these.\n\n- NovaCRM Onboarding Agent"
        )
        self.mail.reply(email, body, reply_to=self.cfg.settings["cs_inbox"])
        self.mail.add_labels(email.message_id, [self.cfg.gmail["labels"]["needs_info"]])
        self.audit.log(run_id, "intake", "clarification_requested", input={"problems": problems},
                       output={"replied_to": email.sender_email}, decision="ASK_AE",
                       rationale="Required fields missing or malformed; asking instead of guessing")
