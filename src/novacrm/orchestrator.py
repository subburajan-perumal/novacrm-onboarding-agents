"""Orchestrator: owns the run state machine and the hand-off from the Intake Agent to the
Communication Agent. Idempotent per Gmail message; parked runs can be resumed."""
from __future__ import annotations

import time
from typing import Optional

from .adapters import MailClient
from .agents.communication import CommunicationAgent
from .agents.intake import IntakeAgent, email_as_text
from .audit import AuditLog
from .config import Config
from .escalation import Escalator
from .models import InboundEmail, OnboardingHandoff, ParsedDeal, RunState, Tier
from .store import RunStore


class Orchestrator:
    def __init__(self, cfg: Config, mail: MailClient, intake: IntakeAgent, comm: CommunicationAgent,
                 store: RunStore, audit: AuditLog, escalator: Escalator):
        self.cfg, self.mail, self.intake, self.comm = cfg, mail, intake, comm
        self.store, self.audit, self.escalator = store, audit, escalator

    # ---- inbox loop ----------------------------------------------------------
    def inbox_query(self) -> str:
        g = self.cfg.gmail
        return f"to:{self.cfg.settings['cs_inbox']} -label:{g['seen_label']} newer_than:{g['lookback']}"

    def poll_once(self) -> list[str]:
        handled = []
        for email in self.mail.list_new(self.inbox_query()):
            run_id = self.handle_email(email)
            if run_id:
                handled.append(run_id)
        return handled

    def watch(self, stop_after: Optional[int] = None) -> None:
        polls = 0
        while stop_after is None or polls < stop_after:
            self.poll_once()
            polls += 1
            time.sleep(self.cfg.gmail["poll_seconds"])

    # ---- one email -----------------------------------------------------------
    def handle_email(self, email: InboundEmail) -> Optional[str]:
        if self.store.get_by_message(email.message_id):
            return None  # idempotent: already processed this exact email
        prior = self.store.open_run_for_thread(email.thread_id, RunState.NEEDS_INFO)
        run = self.store.create(email.message_id, email.thread_id)
        run_id = run["run_id"]
        self.store.update(run_id, data={"email": email.model_dump()})
        self.audit.log(run_id, "orchestrator", "email_received",
                       input={"from": email.sender_email, "subject": email.subject, "message_id": email.message_id},
                       rationale="New message in the CS inbox")
        try:
            if prior:
                self.store.update(prior["run_id"], state=RunState.SUPERSEDED, data={"superseded_by": run_id})
                source_text = self.mail.thread_text(email.thread_id)
                self.audit.log(run_id, "orchestrator", "followup_detected", input={"previous_run": prior["run_id"]},
                               rationale="AE replied on a thread we asked for info on; re-reading the whole thread")
            else:
                source_text = email_as_text(email)
            self._run_pipeline(run_id, email, source_text)
        except Exception as exc:  # last-resort: never lose a deal silently
            self.store.update(run_id, state=RunState.ESCALATED, data={"crash": repr(exc)})
            self.audit.log(run_id, "orchestrator", "unexpected_error", output=repr(exc), decision="ESCALATE")
            self.escalator.escalate(run_id, email, f"Unexpected error: {type(exc).__name__}", repr(exc))
        finally:
            try:
                self.mail.add_labels(email.message_id, [self.cfg.gmail["seen_label"]])
            except Exception as exc:
                self.audit.log(run_id, "orchestrator", "label_failed", output=repr(exc))
        return run_id

    def _run_pipeline(self, run_id: str, email: InboundEmail, source_text: str) -> None:
        deal = self.intake.parse_and_validate(run_id, email, source_text)
        if deal is None:
            return
        tier = self.intake.confirm_tier(run_id, email, deal)
        if tier is None:
            return
        self._project_and_channel(run_id, email, deal, tier)

    def _project_and_channel(self, run_id: str, email: InboundEmail, deal: ParsedDeal, tier: Tier) -> None:
        project = self.intake.create_project(run_id, email, deal, tier)
        if project is None:
            return
        handoff = OnboardingHandoff(run_id=run_id, deal=deal, tier=tier, project=project)
        self.store.update(run_id, data={"handoff": handoff.model_dump(mode="json")})
        self.audit.log(run_id, "orchestrator", "handoff_to_communication", output=handoff,
                       rationale="Project exists; Communication Agent sets up the customer channel")
        try:
            channel = self.comm.run(handoff)
        except Exception as exc:
            self.store.update(run_id, state=RunState.CHANNEL_FAILED, data={"slack_error": repr(exc)})
            self.audit.log(run_id, "communication", "channel_failed", output=repr(exc), decision="ESCALATE")
            self.escalator.escalate(run_id, email, "Slack channel setup failed (project already created)", repr(exc),
                                    customer=deal.customer_name)
            return
        self.store.update(run_id, state=RunState.DONE, channel_id=channel.channel_id)
        self.mail.add_labels(email.message_id, [self.cfg.gmail["labels"]["processed"]])
        self.audit.log(run_id, "orchestrator", "onboarding_started", decision="DONE",
                       output={"project": project.project_url, "channel": channel.channel_name, "tier": tier.value})

    # ---- resume parked runs ----------------------------------------------------
    def resume(self) -> list[str]:
        """Retry runs parked because Rocketlane was down, or whose Slack step failed."""
        resumed = []
        for run in self.store.list([RunState.PARKED_RL_DOWN, RunState.CHANNEL_FAILED]):
            email = InboundEmail(**run["data"]["email"])
            deal = ParsedDeal(**run["data"]["deal"])
            tier = Tier(run["data"]["tier"])
            self.audit.log(run["run_id"], "orchestrator", "resume", input={"state": run["state"]})
            if run["state"] == RunState.PARKED_RL_DOWN.value:
                self._project_and_channel(run["run_id"], email, deal, tier)
            else:
                handoff = OnboardingHandoff(**run["data"]["handoff"])
                channel = self.comm.run(handoff)
                self.store.update(run["run_id"], state=RunState.DONE, channel_id=channel.channel_id)
            resumed.append(run["run_id"])
        return resumed
