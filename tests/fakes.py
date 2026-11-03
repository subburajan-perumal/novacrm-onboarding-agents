"""In-memory fakes for every external system, so the agents run end-to-end offline."""
from __future__ import annotations

import re
from typing import Optional

from novacrm.adapters.rocketlane import RocketlaneUnavailable
from novacrm.models import CallOutcome, DealExtraction, ExtractedField, InboundEmail, TranscriptJudgement


def _f(value: Optional[str]) -> ExtractedField:
    return ExtractedField(value=value, evidence=value)


class FakeLLM:
    """Deterministic stand-in for Claude: a regex extractor over the email text."""

    def __init__(self, override: dict | None = None, welcome: str | None = None):
        self.override = override or {}
        self.welcome = welcome
        self.judge_calls = 0

    def extract_deal(self, text: str) -> DealExtraction:
        def grab(pattern):
            m = re.search(pattern, text, re.I | re.M)
            return m.group(1).strip() if m else None

        is_deal = bool(re.search(r"closed|signed|new deal", text, re.I))
        values = {
            "customer_name": grab(r"^Customer:\s*(.+)$"),
            "customer_contact_name": grab(r"^(?:Primary contact|Contact):\s*([^(,\n]+?)\s*[(,]"),
            "customer_contact_email": grab(r"^(?:Primary contact|Contact):.*?([\w.+-]+@[\w.-]+)"),
            "ae_name": grab(r"^From:\s*([^<\n]+?)\s*<"),
            "salesforce_opportunity_url": grab(r"^(?:Salesforce opportunity|Opportunity):\s*(\S+)"),
            "close_date": grab(r"^Close date:\s*(.+)$"),
            "notes": grab(r"^Notes:\s*(.+)$"),
        }
        values.update(self.override)
        return DealExtraction(
            is_deal_notification=is_deal,
            mentions_plan_tier=bool(re.search(r"\b(enterprise|growth)\b", text, re.I)),
            **{k: _f(v) for k, v in values.items()},
        )

    def judge_transcript(self, transcript: str, customer_name: str) -> TranscriptJudgement:
        self.judge_calls += 1
        t = transcript.lower()
        if any(w in t for w in ("maybe", "i think", "not sure", "probably")):
            return TranscriptJudgement(tier="Unclear", ae_explicitly_confirmed=False, reasoning="hedged")
        named = set(re.findall(r"user:\s*(enterprise|growth)", t))
        if len(named) != 1:
            return TranscriptJudgement(tier="Unclear", ae_explicitly_confirmed=False, reasoning="none or both")
        return TranscriptJudgement(tier=named.pop().title(), ae_explicitly_confirmed="user: yes" in t, reasoning="fake")

    def draft_welcome(self, facts: dict) -> str:
        if self.welcome is not None:
            return self.welcome
        return (f"Welcome {facts['customer company']}! Your {facts['plan tier']} onboarding is a "
                f"{facts['onboarding length']} plan led by {facts['CSM']}. Phases: {facts['phases']}. "
                f"Next: {facts['next step']}.")


def answered(tier: str, confirmed: bool = True) -> CallOutcome:
    yes = "User: Yes, that's right." if confirmed else "User: Hmm."
    return CallOutcome(
        call_id="call_x", status="answered", tier=tier, readback_confirmed=confirmed,
        transcript=f"Agent: Is it Enterprise or Growth?\nUser: {tier}.\nAgent: Just to confirm, {tier}, correct?\n{yes}",
        summary=f"AE said {tier}", raw_reason="retell:agent_hangup",
    )


AMBIGUOUS = CallOutcome(
    call_id="call_amb", status="answered", tier="Unclear", readback_confirmed=False,
    transcript="Agent: Is it Enterprise or Growth?\nUser: I think maybe Growth, not sure, let me check.",
    summary="AE unsure", raw_reason="retell:agent_hangup",
)


class FakeVoice:
    def __init__(self, outcomes: list[CallOutcome]):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def place_call(self, *, to_number, ae_name, customer_name, run_id, attempt) -> CallOutcome:
        self.calls.append({"to": to_number, "attempt": attempt, "customer": customer_name})
        return self.outcomes.pop(0) if self.outcomes else CallOutcome(status="no_answer")


class FakeRocketlane:
    def __init__(self, *, down: bool = False, existing: list[dict] | None = None, task_count: int = 15):
        self.down = down
        self.projects: list[dict] = list(existing or [])
        self.created: list[dict] = []
        self.task_count = task_count
        self.calls = 0

    def _maybe_down(self):
        self.calls += 1
        if self.down:
            raise RocketlaneUnavailable("GET /projects failed after 3 attempts (HTTP 503)")

    def find_existing_projects(self, customer_name, opportunity_id):
        self._maybe_down()
        return [p for p in self.projects
                if p.get("externalReferenceId") == opportunity_id
                or (p.get("customer") or {}).get("companyName", "").lower() == customer_name.lower()]

    def create_project(self, payload):
        self._maybe_down()
        project = {"projectId": 1000 + len(self.projects), "projectName": payload["projectName"],
                   "startDate": payload["startDate"], "dueDate": payload["dueDate"],
                   "customer": payload["customer"], "externalReferenceId": payload.get("externalReferenceId")}
        self.projects.append(project)
        self.created.append(payload)
        return project

    def list_tasks(self, project_id):
        self._maybe_down()
        return [{"taskId": i, "taskName": f"t{i}", "phase": {"phaseName": "Kickoff"}} for i in range(self.task_count)]


class FakeChat:
    def __init__(self, users: dict[str, str] | None = None, existing: dict[str, str] | None = None):
        self.channels: dict[str, dict] = {n: {"id": i, "topic": "", "purpose": "", "messages": []}
                                          for n, i in (existing or {}).items()}
        self.users = users or {}
        self.invites: list[tuple[str, list[str]]] = []

    def _by_id(self, cid):
        return next(c for c in self.channels.values() if c["id"] == cid)

    def find_channel(self, name):
        return self.channels.get(name, {}).get("id")

    def create_channel(self, name):
        cid = f"C{len(self.channels) + 1:04d}"
        self.channels[name] = {"id": cid, "topic": "", "purpose": "", "messages": []}
        return cid

    def set_topic(self, cid, topic):
        self._by_id(cid)["topic"] = topic

    def set_purpose(self, cid, purpose):
        self._by_id(cid)["purpose"] = purpose

    def post_message(self, channel, text):
        self._by_id(channel)["messages"].append(text)

    def lookup_user(self, email):
        return self.users.get(email)

    def invite(self, cid, user_ids):
        self.invites.append((cid, user_ids))


class FakeMail:
    def __init__(self):
        self.replies: list[dict] = []
        self.sent: list[dict] = []
        self.labels: dict[str, set[str]] = {}
        self.inbox: list[InboundEmail] = []

    def list_new(self, query):
        return [e for e in self.inbox if "novacrm-seen" not in self.labels.get(e.message_id, set())]

    def thread_text(self, thread_id):
        return "\n\n---\n\n".join(
            f"From: {e.sender_name} <{e.sender_email}>\nSubject: {e.subject}\n\n{e.body}"
            for e in self.inbox if e.thread_id == thread_id)

    def add_labels(self, message_id, labels):
        self.labels.setdefault(message_id, set()).update(labels)

    def reply(self, email, body, reply_to=None):
        self.replies.append({"to": email.sender_email, "body": body, "reply_to": reply_to})

    def send(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
