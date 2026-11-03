"""Validation: incomplete or malformed emails are rejected; the agent asks, never guesses."""
import pytest

from novacrm.models import RunState

from .conftest import make_email
from .fakes import FakeLLM, answered

NO_CONTACT = """Hi team,
Customer: Acme Logistics
Salesforce opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/0065g00000XyZ12AAB/view
Thanks, closed today."""

INTERNAL_CONTACT = """Closed won!
Customer: Acme Logistics
Contact: Arjun Mehta (arjun.mehta@novacrm.com)
Salesforce opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/0065g00000XyZ12AAB/view"""


def _assert_rejected(s, run, *fragments):
    assert run["state"] == RunState.NEEDS_INFO.value
    assert s.voice.calls == [] and s.rl.created == [] and not s.chat.channels
    assert len(s.mail.replies) == 1
    body = s.mail.replies[0]["body"]
    for f in fragments:
        assert f in body, f
    assert s.mail.replies[0]["reply_to"] == "subburajanperumal+novacrm-cs@gmail.com"
    assert "novacrm-needs-info" in s.mail.labels[run["message_id"]]


def test_missing_customer_contact_and_link(system):
    s = system()
    run = s.run(make_email("missing_fields"))
    _assert_rejected(s, run, "Customer name is missing", "Customer contact email is missing",
                     "Salesforce opportunity link is missing")


def test_no_contact_email(system):
    s = system()
    run = s.run(make_email(body=NO_CONTACT))
    _assert_rejected(s, run, "Customer contact email is missing")


def test_malformed_email_and_link(system):
    s = system()
    run = s.run(make_email("malformed"))
    _assert_rejected(s, run, "daniel.ross@orbit-analytics", "not a Salesforce Opportunity URL")


def test_contact_email_must_be_the_customer(system):
    s = system()
    run = s.run(make_email(body=INTERNAL_CONTACT))
    _assert_rejected(s, run, "points to NovaCRM/the AE")


def test_unknown_sender_is_not_an_ae(system):
    s = system()
    run = s.run(make_email("enterprise_ok", sender="random.person@example.com"))
    _assert_rejected(s, run, "not a registered Account Executive")


def test_hallucinated_value_is_rejected(system):
    s = system(llm=FakeLLM(override={"customer_contact_email": "ceo@acmelogistics.io"}))
    run = s.run(make_email("enterprise_ok"))
    _assert_rejected(s, run, "could not be found verbatim")


def test_non_deal_email_is_ignored(system):
    s = system()
    run = s.run(make_email("not_a_deal"))
    assert run["state"] == RunState.IGNORED.value
    assert s.mail.replies == [] and s.voice.calls == []


def test_ae_reply_with_missing_info_resumes_the_deal(system):
    s = system(outcomes=[answered("Growth")])
    first = make_email(body=NO_CONTACT)
    run1 = s.run(first)
    assert run1["state"] == RunState.NEEDS_INFO.value

    reply = make_email(body="Contact: Meera Nair (meera.nair@acmelogistics.io)", thread_id=first.thread_id)
    reply.subject = "Re: " + first.subject
    run2 = s.run(reply)
    assert s.store.get(run1["run_id"])["state"] == RunState.SUPERSEDED.value
    assert run2["state"] == RunState.DONE.value
    assert s.rl.created[0]["customer"]["companyName"] == "Acme Logistics"


@pytest.mark.parametrize("key", ["missing_fields", "malformed"])
def test_rejected_emails_never_call_the_ae(system, key):
    s = system(outcomes=[answered("Enterprise")])
    s.run(make_email(key))
    assert s.voice.calls == []
