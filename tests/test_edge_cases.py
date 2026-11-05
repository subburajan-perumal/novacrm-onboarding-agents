"""Edge cases: Rocketlane down, existing project, AE doesn't answer, ambiguous tier, crashes, retries."""
import httpx
import pytest

from novacrm.adapters.rocketlane import RocketlaneClient, RocketlaneUnavailable
from novacrm.guardrails import decide_tier
from novacrm.models import CallOutcome, RunState, TranscriptJudgement

from .conftest import make_email
from .fakes import AMBIGUOUS, FakeChat, FakeRocketlane, answered

NO_ANSWER = CallOutcome(call_id="c", status="no_answer", raw_reason="twilio:no-answer")


# ---- Rocketlane API down ----------------------------------------------------
def test_rocketlane_down_parks_run_and_escalates(system):
    s = system(outcomes=[answered("Enterprise")], rocketlane=FakeRocketlane(down=True))
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.PARKED_RL_DOWN.value
    assert s.rl.created == [] and not s.chat.channels
    assert len(s.voice.calls) == 1  # tier confirmed once; not re-asked later
    assert s.mail.sent[0]["to"] == "subburajanperumal+priya@gmail.com"
    assert "Rocketlane API unavailable" in s.mail.sent[0]["subject"]

    s.rl.down = False  # Rocketlane recovers
    assert s.orch.resume() == [run["run_id"]]
    assert s.store.get(run["run_id"])["state"] == RunState.DONE.value
    assert len(s.rl.created) == 1 and len(s.voice.calls) == 1


def test_rocketlane_client_retries_with_backoff_then_gives_up():
    hits, sleeps = [], []
    transport = httpx.MockTransport(lambda req: (hits.append(req), httpx.Response(503))[1])
    client = RocketlaneClient("k", max_retries=3, backoff_seconds=2, sleep=sleeps.append, transport=transport)
    with pytest.raises(RocketlaneUnavailable):
        client.list_tasks(1)
    assert len(hits) == 3 and sleeps == [2, 4]


def test_rocketlane_client_recovers_after_transient_errors():
    responses = iter([httpx.Response(502), httpx.Response(500),
                      httpx.Response(200, json={"data": [{"taskId": 7}], "pagination": {"hasMore": False}})])
    client = RocketlaneClient("k", sleep=lambda s: None, transport=httpx.MockTransport(lambda r: next(responses)))
    assert client.list_tasks(1) == [{"taskId": 7}]


def test_create_project_retries_5xx_when_nothing_was_created():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "pagination": {"hasMore": False}})
        handler.posts += 1
        return httpx.Response(503) if handler.posts < 3 else httpx.Response(201, json={"projectId": 7})
    handler.posts = 0
    client = RocketlaneClient("k", sleep=lambda s: None, transport=httpx.MockTransport(handler))
    assert client.create_project({"projectName": "x", "customer": {"companyName": "c"}})["projectId"] == 7
    assert handler.posts == 3


def test_rocketlane_timeout_counts_as_unavailable():
    def boom(request):
        raise httpx.ConnectTimeout("timed out")
    client = RocketlaneClient("k", sleep=lambda s: None, transport=httpx.MockTransport(boom))
    with pytest.raises(RocketlaneUnavailable):
        client.list_tasks(1)


# ---- template verification ---------------------------------------------------
@pytest.mark.parametrize("rocketlane", [
    FakeRocketlane(task_count=14),
    FakeRocketlane(phases=("Kickoff", "Data Migration", "Configuration")),
], ids=["wrong_task_count", "missing_phase"])
def test_template_mismatch_tells_the_cs_manager_the_project_exists(system, rocketlane):
    s = system(outcomes=[answered("Enterprise")], rocketlane=rocketlane)
    run = s.run(make_email("enterprise_ok"))

    check = next(e for e in s.audit.entries(run["run_id"]) if e["step"] == "template_verification")
    assert check["decision"] == "WARN"
    assert run["state"] == RunState.DONE.value  # the channel still gets set up
    mail = s.mail.sent[0]
    assert mail["to"] == "subburajanperumal+priya@gmail.com"
    assert "already exists" in mail["body"] and "no Rocketlane project" not in mail["body"]


# ---- existing onboarding project ---------------------------------------------
def test_existing_project_is_not_duplicated(system):
    existing = [{"projectId": 42, "projectName": "Acme Logistics - Onboarding (Enterprise)",
                 "customer": {"companyName": "Acme Logistics"},
                 "fields": [{"fieldId": 1, "fieldValue": "https://x.lightning.force.com/0065g00000OLD00AAB"}]}]
    s = system(outcomes=[answered("Enterprise")], rocketlane=FakeRocketlane(existing=existing))
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.DUPLICATE_ESCALATED.value
    assert s.rl.created == [] and not s.chat.channels
    assert "already has an onboarding project" in s.mail.sent[0]["subject"]
    assert "#42" in s.mail.sent[0]["body"]


def test_same_run_retry_adopts_its_own_project(system):
    """Crash after the POST succeeded: re-running must adopt, not create a second project."""
    s = system(outcomes=[answered("Enterprise")])
    email = make_email("enterprise_ok")
    run = s.run(email)
    deal = s.intake.store.get(run["run_id"])["data"]["deal"]
    from novacrm.models import ParsedDeal, Tier
    again = s.intake.create_project(run["run_id"], email, ParsedDeal(**deal), Tier.ENTERPRISE)
    assert again is not None and len(s.rl.created) == 1
    assert "project_adopted" in s.steps(run["run_id"])


# ---- AE doesn't answer ------------------------------------------------------
def test_no_answer_retries_then_escalates(system):
    s = system(outcomes=[NO_ANSWER, NO_ANSWER, NO_ANSWER])
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.ESCALATED.value
    assert [c["attempt"] for c in s.voice.calls] == [1, 2, 3]
    assert s.rl.created == [] and not s.chat.channels
    assert "not reached after 3 attempts" in s.mail.sent[0]["subject"]
    assert s.steps(run["run_id"]).count("voice_retry_scheduled") == 2


def test_voicemail_then_answer_succeeds(system):
    vm = CallOutcome(call_id="c", status="voicemail", raw_reason="retell:voicemail_reached")
    s = system(outcomes=[vm, answered("Growth")])
    run = s.run(make_email("growth_ok"))
    assert run["state"] == RunState.DONE.value and run["tier"] == "Growth"
    assert len(s.voice.calls) == 2


def test_telephony_exception_is_a_failed_attempt_not_a_tier(system):
    class Exploding:
        calls = []
        def place_call(self, **kw):
            self.calls.append(kw)
            raise RuntimeError("twilio 500")
    s = system()
    s.intake.voice = Exploding()
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.ESCALATED.value and len(Exploding.calls) == 3
    assert s.rl.created == []


# ---- ambiguous tier -----------------------------------------------------------
def test_ambiguous_answer_recalls_then_escalates(system):
    s = system(outcomes=[AMBIGUOUS, AMBIGUOUS])
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.ESCALATED.value
    assert len(s.voice.calls) == 2
    assert s.rl.created == [] and not s.chat.channels
    assert "Plan tier not confirmed" in s.mail.sent[0]["subject"]
    assert "not sure" in s.mail.sent[0]["body"]  # transcript included for the human


def test_ambiguous_then_clear_answer_proceeds(system):
    s = system(outcomes=[AMBIGUOUS, answered("Enterprise")])
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.DONE.value and len(s.voice.calls) == 2


def test_tier_without_readback_confirmation_is_not_accepted(system):
    s = system(outcomes=[answered("Enterprise", confirmed=False), answered("Enterprise", confirmed=False)])
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.ESCALATED.value and s.rl.created == []


def test_voice_agent_and_transcript_check_must_agree(system):
    disagree = answered("Enterprise")
    disagree.transcript = "Agent: Enterprise or Growth?\nUser: Growth.\nAgent: Growth, correct?\nUser: Yes."
    s = system(outcomes=[disagree, answered("Growth")])
    run = s.run(make_email("growth_ok"))
    assert len(s.voice.calls) == 2 and run["tier"] == "Growth"


# ---- other failures -------------------------------------------------------------
def test_slack_failure_after_project_is_resumable(system):
    class FlakyChat(FakeChat):
        fail = True
        def create_channel(self, name):
            if self.fail:
                raise RuntimeError("slack: ratelimited")
            return super().create_channel(name)
    chat = FlakyChat(users={})
    s = system(outcomes=[answered("Enterprise")], chat=chat)
    run = s.run(make_email("enterprise_ok"))
    assert run["state"] == RunState.CHANNEL_FAILED.value and len(s.rl.created) == 1
    assert "already exists" in s.mail.sent[0]["body"] and "no Rocketlane project" not in s.mail.sent[0]["body"]
    chat.fail = False
    s.orch.resume()
    assert s.store.get(run["run_id"])["state"] == RunState.DONE.value
    assert len(s.rl.created) == 1  # project not recreated


def test_same_email_is_processed_once(system):
    s = system(outcomes=[answered("Enterprise")])
    email = make_email("enterprise_ok")
    s.run(email)
    assert s.orch.handle_email(email) is None
    assert len(s.rl.created) == 1 and len(s.voice.calls) == 1


def test_template_not_configured_escalates(system, cfg):
    cfg.settings["tiers"]["Growth"]["template_id"] = 0
    s = system(outcomes=[answered("Growth")])
    run = s.run(make_email("growth_ok"))
    assert run["state"] == RunState.ESCALATED.value and s.rl.created == []


# ---- decide_tier unit checks ------------------------------------------------------
@pytest.mark.parametrize("outcome,judge,attempt,unclear,action", [
    (answered("Enterprise"), TranscriptJudgement(tier="Enterprise", ae_explicitly_confirmed=True, reasoning=""), 1, 0, "accept"),
    (answered("Growth"), TranscriptJudgement(tier="Enterprise", ae_explicitly_confirmed=True, reasoning=""), 1, 1, "retry"),
    (AMBIGUOUS, TranscriptJudgement(tier="Unclear", ae_explicitly_confirmed=False, reasoning=""), 2, 2, "escalate"),
    (NO_ANSWER, None, 3, 0, "escalate"),
    (answered("Enterprise"), None, 1, 1, "retry"),  # transcript check unavailable -> never accept
])
def test_decide_tier(cfg, outcome, judge, attempt, unclear, action):
    assert decide_tier(outcome, judge, attempt, unclear, cfg).action == action


def test_timed_out_create_is_not_reposted_when_project_exists():
    """Regression: a slow template import made POST /projects time out; blind retries created duplicates."""
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"data": [{"projectId": 99, "projectName": "Acme - Onboarding (Enterprise)",
                                                    "customer": {"companyName": "Acme"}}],
                                         "pagination": {"hasMore": False}})
    client = RocketlaneClient("k", sleep=lambda s: None, transport=httpx.MockTransport(handler))
    created = client.create_project({"projectName": "Acme - Onboarding (Enterprise)", "customer": {"companyName": "Acme"}})
    assert created["projectId"] == 99 and len(posts) == 1
