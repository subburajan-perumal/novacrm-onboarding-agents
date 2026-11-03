"""Happy path: Enterprise deal email -> parse -> voice confirms -> Rocketlane project -> Slack channel."""
from novacrm.models import RunState

from .conftest import make_email
from .fakes import answered


def test_enterprise_deal_end_to_end(system):
    s = system(outcomes=[answered("Enterprise")])
    run = s.run(make_email("enterprise_ok"))

    assert run["state"] == RunState.DONE.value
    assert run["tier"] == "Enterprise"
    assert run["customer"] == "Acme Logistics"

    # exactly one call, to the AE's phone from the directory
    assert len(s.voice.calls) == 1
    assert s.voice.calls[0]["to"] == "+919999900000"

    # Rocketlane project from the Enterprise template
    assert len(s.rl.created) == 1
    p = s.rl.created[0]
    assert p["sources"] == [{"templateId": 501, "startDate": "2026-11-03"}]
    assert p["projectName"] == "Acme Logistics - Onboarding (Enterprise)"
    assert p["customer"] == {"companyName": "Acme Logistics"}
    assert p["teamMembers"]["members"] == [{"emailId": "sampleuser1@rocketlane.com"}]
    assert p["dueDate"] == "2026-12-02"

    # personalized Slack channel
    ch = s.chat.channels["onb-acme-logistics-ent"]
    assert ch["topic"].startswith("Enterprise onboarding · 30-day plan · Dedicated CSM: Rachel")
    assert "Acme Logistics" in ch["purpose"] and "rocketlane.com/projects/" in ch["purpose"]
    welcome = ch["messages"][0]
    assert "Acme Logistics" in welcome and "30 days" in welcome and "Rachel" in welcome
    assert any("U_KAVYA" in ids and "U_AE" in ids for _, ids in s.chat.invites)

    # inbox bookkeeping + nobody escalated
    assert {"novacrm-seen", "novacrm-processed"} <= s.mail.labels[run["message_id"]]
    assert s.mail.sent == [] and s.mail.replies == []


def test_every_action_is_audited(system):
    s = system(outcomes=[answered("Enterprise")])
    run = s.run(make_email("enterprise_ok"))
    entries = s.audit.entries(run["run_id"])
    steps = [e["step"] for e in entries]
    for expected in ["email_received", "extracted_fields", "required_fields_check", "voice_call_started",
                     "voice_call_result", "duplicate_check", "project_created", "template_verification",
                     "handoff_to_communication", "channel_ready", "welcome_message_check",
                     "customer_invite_mocked", "onboarding_started"]:
        assert expected in steps, expected
    for e in entries:
        assert set(e) == {"ts", "run_id", "agent", "step", "input", "output", "decision", "rationale"}
    call = next(e for e in entries if e["step"] == "voice_call_result")
    assert call["decision"] == "ACCEPT" and "confirmed Enterprise" in call["rationale"]
