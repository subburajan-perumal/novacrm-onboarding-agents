"""Template accuracy: Enterprise -> 30-day template + dedicated CSM; Growth -> 14-day template + pooled CSM."""
from novacrm.models import RunState

from .conftest import make_email
from .fakes import FakeLLM, answered


def test_enterprise_gets_30_day_template_and_dedicated_csm(system):
    s = system(outcomes=[answered("Enterprise")])
    s.run(make_email("enterprise_ok"))
    p = s.rl.created[0]
    assert p["sources"][0]["templateId"] == 501
    assert (p["startDate"], p["dueDate"]) == ("2026-11-03", "2026-12-02")  # 30 days inclusive
    assert p["teamMembers"]["members"][0]["emailId"] == "subburajanperumal+csm.kavya@gmail.com"
    topic = s.chat.channels["onb-acme-logistics-ent"]["topic"]
    assert "30-day plan" in topic and "Dedicated CSM" in topic


def test_growth_gets_14_day_template_and_pooled_csm(system):
    s = system(outcomes=[answered("Growth")])
    run = s.run(make_email("growth_ok"))
    assert run["state"] == RunState.DONE.value
    p = s.rl.created[0]
    assert p["sources"][0]["templateId"] == 502
    assert (p["startDate"], p["dueDate"]) == ("2026-11-03", "2026-11-16")  # 14 days inclusive
    assert p["teamMembers"]["members"][0]["emailId"] == "subburajanperumal+csm.pool@gmail.com"
    ch = s.chat.channels["onb-brightpath-dental-grw"]
    assert ch["topic"].startswith("Growth onboarding · 14-day plan · Pooled CSM: Growth CSM Pool")
    assert "dedicated" not in ch["messages"][0].lower()


def test_dedicated_csms_rotate_round_robin(system):
    s = system(outcomes=[answered("Enterprise"), answered("Enterprise")])
    s.run(make_email("enterprise_ok"))
    second = make_email("enterprise_ok", body=make_email().body.replace("Acme Logistics", "Nimbus Retail")
                        .replace("0065g00000XyZ12AAB", "0065g00000QqQ99AAB"))
    s.run(second)
    members = [c["teamMembers"]["members"][0]["emailId"] for c in s.rl.created]
    assert members == ["subburajanperumal+csm.kavya@gmail.com", "subburajanperumal+csm.rahul@gmail.com"]


def test_tier_in_email_body_is_not_trusted(system):
    """'Plan: Enterprise' in the email doesn't skip the call; the AE's spoken answer wins."""
    body = make_email("enterprise_ok").body + "\nPlan: Enterprise\n"
    s = system(outcomes=[answered("Growth")])
    run = s.run(make_email(body=body))
    assert len(s.voice.calls) == 1
    assert run["tier"] == "Growth"
    assert s.rl.created[0]["sources"][0]["templateId"] == 502
    assert "tier_in_email_ignored" in s.steps(run["run_id"])


def test_welcome_message_with_wrong_tier_falls_back_to_template(system):
    bad = "Welcome Acme Logistics! Your Growth plan is a 14-day onboarding with Kavya Raman."
    s = system(outcomes=[answered("Enterprise")], llm=FakeLLM(welcome=bad))
    run = s.run(make_email("enterprise_ok"))
    msg = s.chat.channels["onb-acme-logistics-ent"]["messages"][0]
    assert "30-day" in msg and "Growth" not in msg
    check = next(e for e in s.audit.entries(run["run_id"]) if e["step"] == "welcome_message_check")
    assert check["decision"] == "FALLBACK"
