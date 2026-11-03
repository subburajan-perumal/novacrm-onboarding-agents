"""Real outbound voice call: Retell AI voice agent, carried over Twilio to the AE's phone.

Retell-purchased numbers only dial US destinations, so we use Retell's "dial to SIP URI" custom
telephony: register the call with Retell, then have Twilio ring the AE and bridge the answered
call to sip:{call_id}@sip.retellai.com. Twilio tells us no-answer/busy cleanly; Retell gives us
the transcript and the post-call structured analysis (plan_tier, readback_confirmed).
"""
from __future__ import annotations

import time
from typing import Callable

import httpx
from twilio.rest import Client as TwilioClient

from ..models import CallOutcome

RETELL_BASE = "https://api.retellai.com"

AGENT_PROMPT = """You are Nova, NovaCRM's onboarding assistant. You are calling {{ae_name}}, an Account Executive at NovaCRM, about a newly closed deal with {{customer_name}}.

Your only job is to confirm whether {{customer_name}} bought the Enterprise plan or the Growth plan.

Steps:
1. Say in one sentence why you are calling.
2. Ask: "Is {{customer_name}} on the Enterprise plan or the Growth plan?"
3. When they name one plan, read it back: "Just to confirm, {{customer_name}} is on the <plan> plan, correct?" and wait for an explicit yes.
4. After an explicit yes, thank them, say onboarding will start right away, and end the call.

Rules:
- Never suggest, assume or pick a plan for them.
- If the answer is unclear, hedged ("maybe", "I think", "not sure"), names both plans, or they don't know, ask once more. If it is still unclear, say a teammate from Customer Success will follow up, and end the call.
- If it is a bad time or they need to check, thank them, say the team will follow up, and end the call.
- Keep each turn under two short sentences. Do not discuss pricing, contracts or anything else."""

BEGIN_MESSAGE = "Hi {{ae_name}}, this is Nova, NovaCRM's onboarding assistant, calling about the {{customer_name}} deal. Do you have ten seconds?"

POST_CALL_ANALYSIS = [
    {
        "type": "enum",
        "name": "plan_tier",
        "description": (
            "The plan tier the AE stated for the customer AND confirmed on read-back. "
            "Use Unclear if the AE hedged, named both plans, did not know, refused, or never confirmed the read-back."
        ),
        "choices": ["Enterprise", "Growth", "Unclear"],
    },
    {
        "type": "boolean",
        "name": "readback_confirmed",
        "description": "True only if the agent read the tier back and the AE explicitly said yes to it.",
    },
    {"type": "system-presets", "name": "call_summary"},
]


def setup_retell_agent(api_key: str, voice_id: str = "retell-Cimo") -> dict:
    """Create the Retell LLM + agent once. Returns {'llm_id', 'agent_id'}."""
    http = httpx.Client(base_url=RETELL_BASE, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    llm = http.post("/create-retell-llm", json={
        "general_prompt": AGENT_PROMPT,
        "begin_message": BEGIN_MESSAGE,
        "start_speaker": "agent",
        "default_dynamic_variables": {"ae_name": "there", "customer_name": "the customer"},
        "general_tools": [{"type": "end_call", "name": "end_call", "description": "End the call when done."}],
    })
    llm.raise_for_status()
    llm_id = llm.json()["llm_id"]
    agent = http.post("/create-agent", json={
        "agent_name": "NovaCRM Tier Confirmation",
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": voice_id,
        "language": "en-US",
        "end_call_after_silence_ms": 15000,
        "max_call_duration_ms": 180000,
        "voicemail_option": {"action": {"type": "hangup"}},
        "post_call_analysis_data": POST_CALL_ANALYSIS,
    })
    agent.raise_for_status()
    return {"llm_id": llm_id, "agent_id": agent.json()["agent_id"]}


class RetellTwilioVoice:
    def __init__(
        self,
        *,
        retell_api_key: str,
        agent_id: str,
        twilio_sid: str,
        twilio_token: str,
        from_number: str,
        ring_timeout: int = 25,
        poll_seconds: float = 3,
        max_wait_seconds: int = 300,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.retell = httpx.Client(base_url=RETELL_BASE, headers={"Authorization": f"Bearer {retell_api_key}"}, timeout=30)
        self.twilio = TwilioClient(twilio_sid, twilio_token)
        self.agent_id = agent_id
        self.from_number = from_number
        self.ring_timeout = ring_timeout
        self.poll = poll_seconds
        self.max_wait = max_wait_seconds
        self.sleep = sleep

    def place_call(self, *, to_number: str, ae_name: str, customer_name: str, run_id: str, attempt: int) -> CallOutcome:
        reg = self.retell.post("/v2/register-phone-call", json={
            "agent_id": self.agent_id,
            "from_number": self.from_number,
            "to_number": to_number,
            "direction": "outbound",
            "metadata": {"run_id": run_id, "attempt": attempt},
            "retell_llm_dynamic_variables": {"ae_name": ae_name.split()[0], "customer_name": customer_name},
        })
        reg.raise_for_status()
        call_id = reg.json()["call_id"]

        twiml = f"<Response><Dial><Sip>sip:{call_id}@sip.retellai.com</Sip></Dial></Response>"
        tw = self.twilio.calls.create(to=to_number, from_=self.from_number, twiml=twiml, timeout=self.ring_timeout)

        status = self._wait_twilio(tw.sid)
        if status == "no-answer":
            return CallOutcome(call_id=call_id, status="no_answer", raw_reason="twilio:no-answer")
        if status == "busy":
            return CallOutcome(call_id=call_id, status="busy", raw_reason="twilio:busy")
        if status in ("failed", "canceled", "timeout"):
            return CallOutcome(call_id=call_id, status="failed", raw_reason=f"twilio:{status}")

        call = self._wait_retell(call_id)
        reason = call.get("disconnection_reason") or call.get("call_status", "")
        analysis = call.get("call_analysis") or {}
        if call.get("call_status") in ("registered", "not_connected") or reason == "registered_call_timeout":
            # Twilio answered but never bridged to the agent (e.g. trial key not pressed, AE hung up instantly)
            return CallOutcome(call_id=call_id, status="no_answer", raw_reason=f"retell:{reason or 'not bridged'}")
        if reason == "voicemail_reached" or analysis.get("in_voicemail"):
            return CallOutcome(call_id=call_id, status="voicemail", raw_reason=f"retell:{reason}")
        if reason in ("dial_no_answer", "user_declined"):
            return CallOutcome(call_id=call_id, status="no_answer", raw_reason=f"retell:{reason}")
        if str(reason).startswith("error") or call.get("call_status") == "error":
            return CallOutcome(call_id=call_id, status="failed", raw_reason=f"retell:{reason}",
                               transcript=call.get("transcript", ""))

        custom = analysis.get("custom_analysis_data") or {}
        return CallOutcome(
            call_id=call_id,
            status="answered",
            tier=custom.get("plan_tier"),
            readback_confirmed=bool(custom.get("readback_confirmed")),
            transcript=call.get("transcript", ""),
            summary=analysis.get("call_summary", ""),
            raw_reason=f"retell:{reason}",
        )

    def _wait_twilio(self, sid: str) -> str:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            status = self.twilio.calls(sid).fetch().status
            if status in ("completed", "busy", "no-answer", "failed", "canceled"):
                return status
            self.sleep(self.poll)
        return "timeout"

    def _wait_retell(self, call_id: str) -> dict:
        """Wait for the call to end and for post-call analysis to be attached."""
        deadline = time.monotonic() + 90
        call: dict = {}
        while time.monotonic() < deadline:
            resp = self.retell.get(f"/v2/get-call/{call_id}")
            resp.raise_for_status()
            call = resp.json()
            status = call.get("call_status")
            if status == "error":
                return call
            if status == "ended" and call.get("call_analysis") is not None:
                return call
            if status in ("registered", "not_connected") and time.monotonic() > deadline - 70:
                return call  # Twilio already finished; this call was never bridged
            self.sleep(self.poll)
        return call
