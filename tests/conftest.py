from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
import yaml

from novacrm.agents.communication import CommunicationAgent
from novacrm.agents.intake import IntakeAgent
from novacrm.audit import AuditLog
from novacrm.config import load_config
from novacrm.escalation import Escalator
from novacrm.models import InboundEmail
from novacrm.orchestrator import Orchestrator
from novacrm.store import RunStore

from .fakes import FakeChat, FakeLLM, FakeMail, FakeRocketlane, FakeVoice

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = yaml.safe_load((ROOT / "samples" / "deals.yaml").read_text(encoding="utf-8"))
AE_EMAIL = "subburajanperumal@gmail.com"
TODAY = date(2026, 11, 3)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AE_PHONE", "+919999900000")
    c = load_config()
    c.settings["tiers"]["Enterprise"]["template_id"] = 501
    c.settings["tiers"]["Growth"]["template_id"] = 502
    c.settings["voice"]["retry_delay_seconds"] = 0
    return c


def make_email(key: str = "enterprise_ok", sender: str = AE_EMAIL, body: str | None = None,
               thread_id: str | None = None) -> InboundEmail:
    s = SAMPLES[key]
    return InboundEmail(message_id="m-" + uuid.uuid4().hex[:8], thread_id=thread_id or "t-" + uuid.uuid4().hex[:8],
                        sender_email=sender, sender_name="Subburajan Perumal",
                        to="subburajanperumal+novacrm-cs@gmail.com", subject=s["subject"],
                        body=body if body is not None else s["body"])


class System:
    def __init__(self, cfg, tmp_path, *, outcomes=None, rocketlane=None, chat=None, llm=None):
        self.cfg = cfg
        self.mail = FakeMail()
        self.llm = llm or FakeLLM()
        self.voice = FakeVoice(outcomes or [])
        self.rl = rocketlane or FakeRocketlane()
        self.chat = chat or FakeChat(users={"sampleuser1@rocketlane.com": "U_KAVYA",
                                            "subburajan.dev@gmail.com": "U_AE"})  # AE's Slack alias
        self.audit = AuditLog(tmp_path / "audit.jsonl", echo=False)
        self.store = RunStore(":memory:")
        self.escalator = Escalator(cfg, self.mail, self.audit, self.chat)
        self.intake = IntakeAgent(cfg, self.llm, self.voice, self.rl, self.store, self.audit, self.escalator,
                                  sleep=lambda s: None, today=lambda: TODAY)
        self.comm = CommunicationAgent(cfg, self.chat, self.llm, self.audit)
        self.orch = Orchestrator(cfg, self.mail, self.intake, self.comm, self.store, self.audit, self.escalator)

    def run(self, email: InboundEmail) -> dict:
        self.mail.inbox.append(email)
        run_id = self.orch.handle_email(email)
        return self.store.get(run_id)

    def steps(self, run_id: str) -> list[str]:
        return [e["step"] for e in self.audit.entries(run_id)]


@pytest.fixture
def system(cfg, tmp_path):
    def _make(**kw):
        return System(cfg, tmp_path, **kw)
    return _make
