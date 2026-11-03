"""Agent 1 - Intake & Routing.

parse (Claude) -> validate (guardrails) -> confirm tier by voice call (Retell) -> create the
Rocketlane project from the tier's template. Each step is logged; any doubt goes to a human.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Callable, Optional

from ..adapters import LLMClient, ProjectClient, VoiceClient
from ..adapters.rocketlane import RocketlaneError, RocketlaneUnavailable
from ..audit import AuditLog
from ..config import CSM, Config
from ..escalation import Escalator
from ..guardrails import decide_tier, validate_deal
from ..models import CallOutcome, InboundEmail, ParsedDeal, ProjectResult, RunState, Tier, TranscriptJudgement
from ..store import RunStore

AGENT = "intake"


def email_as_text(email: InboundEmail) -> str:
    return (f"From: {email.sender_name} <{email.sender_email}>\nTo: {email.to}\n"
            f"Subject: {email.subject}\nDate: {email.received_at}\n\n{email.body}")


class IntakeAgent:
    def __init__(
        self,
        cfg: Config,
        llm: LLMClient,
        voice: VoiceClient,
        projects: ProjectClient,
        store: RunStore,
        audit: AuditLog,
        escalator: Escalator,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ):
        self.cfg, self.llm, self.voice, self.projects = cfg, llm, voice, projects
        self.store, self.audit, self.escalator = store, audit, escalator
        self.sleep, self.today = sleep, today

    # ---- 1. parse + validate ----------------------------------------------
    def parse_and_validate(self, run_id: str, email: InboundEmail, source_text: str) -> Optional[ParsedDeal]:
        try:
            extraction = self.llm.extract_deal(source_text)
        except Exception as exc:
            self.audit.log(run_id, AGENT, "extract_failed", output=str(exc), decision="ESCALATE",
                           rationale="LLM extraction failed; a human must read this email")
            self.store.update(run_id, state=RunState.ESCALATED)
            self.escalator.escalate(run_id, email, f"Could not parse the deal email ({type(exc).__name__})")
            return None
        self.audit.log(run_id, AGENT, "extracted_fields", input={"subject": email.subject, "from": email.sender_email},
                       output=extraction, rationale="Claude structured extraction with verbatim evidence per field")

        if not extraction.is_deal_notification:
            self.store.update(run_id, state=RunState.IGNORED)
            self.audit.log(run_id, AGENT, "not_a_deal_email", decision="IGNORE",
                           rationale="Email is not a closed-won deal notification")
            return None

        deal, result = validate_deal(extraction, email, source_text, self.cfg)
        self.audit.log(run_id, "guardrail", "required_fields_check", input={"fields": extraction},
                       output=result, decision="PASS" if result.ok else "FAIL",
                       rationale="; ".join(result.problems) or "All required fields present, grounded and well-formed")
        if not result.ok:
            self.store.update(run_id, state=RunState.NEEDS_INFO, data={"problems": result.problems})
            self.escalator.ask_ae_for_info(run_id, email, result.problems)
            return None
        if extraction.mentions_plan_tier:
            self.audit.log(run_id, "guardrail", "tier_in_email_ignored", decision="CALL_ANYWAY",
                           rationale="Tier must be confirmed verbally by the AE; email text is not trusted for tier")
        self.store.update(run_id, state=RunState.VALIDATED, customer=deal.customer_name,
                          data={"deal": deal.model_dump()})
        return deal

    # ---- 2. voice confirmation --------------------------------------------
    def confirm_tier(self, run_id: str, email: InboundEmail, deal: ParsedDeal) -> Optional[Tier]:
        attempt = unclear = 0
        last_reason = ""
        while True:
            attempt += 1
            self.store.update(run_id, state=RunState.CALLING, call_attempts=attempt)
            self.audit.log(run_id, AGENT, "voice_call_started",
                           input={"to": deal.ae_phone[:-4] + "****", "ae": deal.ae_name, "attempt": attempt},
                           rationale="Plan tier is not in the email; confirming with the AE by phone")
            try:
                outcome = self.voice.place_call(to_number=deal.ae_phone, ae_name=deal.ae_name,
                                                customer_name=deal.customer_name, run_id=run_id, attempt=attempt)
            except Exception as exc:  # telephony/API error counts as a failed attempt, never as a tier
                outcome = CallOutcome(status="failed", raw_reason=f"{type(exc).__name__}: {exc}")

            judgement: Optional[TranscriptJudgement] = None
            if outcome.status == "answered":
                try:
                    judgement = self.llm.judge_transcript(outcome.transcript, deal.customer_name)
                except Exception as exc:
                    self.audit.log(run_id, AGENT, "transcript_check_failed", output=str(exc))
                if not (outcome.tier in ("Enterprise", "Growth") and outcome.readback_confirmed):
                    unclear += 1
                elif judgement is None or judgement.tier != outcome.tier or not judgement.ae_explicitly_confirmed:
                    unclear += 1

            decision = decide_tier(outcome, judgement, attempt, unclear, self.cfg)
            self.audit.log(run_id, AGENT, "voice_call_result",
                           input={"call_id": outcome.call_id, "attempt": attempt},
                           output={"status": outcome.status, "voice_tier": outcome.tier,
                                   "readback_confirmed": outcome.readback_confirmed,
                                   "transcript_check": judgement, "summary": outcome.summary,
                                   "reason": outcome.raw_reason, "transcript": outcome.transcript},
                           decision=decision.action.upper(), rationale=decision.reason)
            last_reason = decision.reason

            if decision.action == "accept":
                self.store.update(run_id, state=RunState.TIER_CONFIRMED, tier=decision.tier.value,
                                  data={"tier": decision.tier.value, "call_id": outcome.call_id})
                return decision.tier
            if decision.action == "escalate":
                self.store.update(run_id, state=RunState.ESCALATED, data={"escalation": last_reason})
                details = f"Last call transcript:\n{outcome.transcript or '(no conversation)'}"
                self.escalator.escalate(run_id, email, f"Plan tier not confirmed: {last_reason}", details,
                                        customer=deal.customer_name)
                return None
            delay = self.cfg.voice["retry_delay_seconds"]
            self.audit.log(run_id, AGENT, "voice_retry_scheduled", output={"in_seconds": delay, "next_attempt": attempt + 1})
            self.sleep(delay)

    # ---- 3. Rocketlane project --------------------------------------------
    def _pick_csm(self, tier: Tier) -> CSM:
        if tier == Tier.GROWTH:
            return self.cfg.pooled_csm
        n = len([r for r in self.store.list() if r.get("tier") == Tier.ENTERPRISE.value and r.get("project_id")])
        return self.cfg.dedicated_csms[n % len(self.cfg.dedicated_csms)]

    def build_payload(self, deal: ParsedDeal, tier: Tier, csm: CSM, start: date) -> dict:
        tcfg = self.cfg.tier(tier.value)
        rl = self.cfg.rocketlane
        due = start + timedelta(days=tcfg["duration_days"] - 1)
        payload = {
            "projectName": f"{deal.customer_name} - Onboarding ({tier.value})",
            "owner": {"emailId": rl["project_owner_email"]},
            "customer": {"companyName": deal.customer_name},
            "autoCreateCompany": True,
            "startDate": start.isoformat(),
            "dueDate": due.isoformat(),
            "externalReferenceId": deal.salesforce_opportunity_id,
            "teamMembers": {"members": [{"emailId": csm.email}]},
            "sources": [{"templateId": int(tcfg["template_id"]), "startDate": start.isoformat()}],
        }
        if rl.get("invite_customer"):
            payload["teamMembers"]["customers"] = [{"emailId": deal.contact_email}]
        pm_placeholder = int(rl.get("project_manager_placeholder_id") or 0)
        if pm_placeholder:
            payload["placeholders"] = [{"placeholderId": pm_placeholder, "user": {"emailId": csm.email}}]
        fids = rl.get("field_ids", {})
        values = {"plan_tier": tier.value, "csm_model": tcfg["csm_model"],
                  "salesforce_opportunity": deal.salesforce_url, "account_executive": deal.ae_name}
        fields = [{"fieldId": int(fids[k]), "fieldValue": v} for k, v in values.items() if int(fids.get(k) or 0)]
        if fields:
            payload["fields"] = fields
        return payload

    def create_project(self, run_id: str, email: InboundEmail, deal: ParsedDeal, tier: Tier) -> Optional[ProjectResult]:
        run = self.store.get(run_id)
        tcfg = self.cfg.tier(tier.value)
        if not int(tcfg.get("template_id") or 0):
            self.audit.log(run_id, "guardrail", "template_not_configured", decision="ESCALATE",
                           rationale=f"No Rocketlane template id configured for {tier.value}")
            self.store.update(run_id, state=RunState.ESCALATED)
            self.escalator.escalate(run_id, email, f"No Rocketlane template configured for {tier.value}",
                                    customer=deal.customer_name)
            return None
        try:
            existing = self.projects.find_existing_projects(deal.customer_name, deal.salesforce_opportunity_id)
            self.audit.log(run_id, AGENT, "duplicate_check",
                           input={"customer": deal.customer_name, "opportunity": deal.salesforce_opportunity_id},
                           output=[{"projectId": p.get("projectId"), "projectName": p.get("projectName")} for p in existing],
                           decision="FOUND" if existing else "NONE")
            adopt = None
            if existing:
                same_run = run["data"].get("project_create_attempted") and any(
                    p.get("externalReferenceId") == deal.salesforce_opportunity_id for p in existing)
                if same_run:  # our own earlier attempt succeeded before a crash: adopt it, don't duplicate
                    adopt = next(p for p in existing if p.get("externalReferenceId") == deal.salesforce_opportunity_id)
                else:
                    names = ", ".join(f"#{p.get('projectId')} {p.get('projectName')}" for p in existing)
                    self.store.update(run_id, state=RunState.DUPLICATE_ESCALATED, data={"existing": names})
                    self.escalator.escalate(run_id, email, "Customer already has an onboarding project",
                                            f"Existing: {names}", customer=deal.customer_name)
                    return None

            csm = self._pick_csm(tier)
            start = self.today()
            if adopt:
                created = adopt
                self.audit.log(run_id, AGENT, "project_adopted", output={"projectId": adopt.get("projectId")},
                               decision="ADOPT", rationale="Project from this run's earlier attempt already exists")
            else:
                payload = self.build_payload(deal, tier, csm, start)
                self.store.update(run_id, data={"project_create_attempted": True})
                created = self.projects.create_project(payload)
                self.audit.log(run_id, AGENT, "project_created", input=payload,
                               output={k: created.get(k) for k in ("projectId", "projectName", "startDate", "dueDate")},
                               decision="CREATED",
                               rationale=f"{tier.value}: template {tcfg['template_id']}, {tcfg['duration_days']} days, "
                                         f"{tcfg['csm_model']} CSM {csm.name}")
            project_id = int(created["projectId"])
            self.store.update(run_id, state=RunState.PROJECT_CREATED, project_id=project_id)

            task_count = None
            try:
                tasks = self.projects.list_tasks(project_id)
                task_count = len(tasks)
                expected = self.cfg.rocketlane.get("expected_task_count")
                ok = expected is None or task_count == expected
                self.audit.log(run_id, "guardrail", "template_verification",
                               output={"tasks": task_count, "expected": expected,
                                       "phases": sorted({(t.get("phase") or {}).get("phaseName", "") for t in tasks})},
                               decision="PASS" if ok else "WARN",
                               rationale="Template import produced the expected task set" if ok
                               else "Task count differs from the template definition; CS manager notified")
            except RocketlaneUnavailable as exc:
                self.audit.log(run_id, "guardrail", "template_verification_skipped", output=str(exc))

            start_d = date.fromisoformat(str(created.get("startDate") or start.isoformat()))
            due_d = date.fromisoformat(str(created.get("dueDate") or (start + timedelta(days=tcfg['duration_days'] - 1)).isoformat()))
            return ProjectResult(
                project_id=project_id,
                project_name=created.get("projectName", ""),
                project_url=f"{self.cfg.rocketlane['app_url']}/projects/{project_id}",
                template_id=int(tcfg["template_id"]),
                start_date=start_d,
                due_date=due_d,
                csm_name=csm.name,
                csm_email=csm.email,
                csm_model=tcfg["csm_model"],
                task_count=task_count,
            )
        except RocketlaneUnavailable as exc:
            self.store.update(run_id, state=RunState.PARKED_RL_DOWN, data={"tier": tier.value, "rl_error": str(exc)})
            self.audit.log(run_id, AGENT, "rocketlane_unavailable", output=str(exc), decision="PARK",
                           rationale="Retries exhausted; run parked for `novacrm resume`, CS manager notified")
            self.escalator.escalate(run_id, email, "Rocketlane API unavailable; onboarding parked",
                                    f"{exc}\nThe run resumes automatically with `novacrm resume`.",
                                    customer=deal.customer_name)
            return None
        except RocketlaneError as exc:
            self.store.update(run_id, state=RunState.ESCALATED, data={"rl_error": str(exc)})
            self.audit.log(run_id, AGENT, "rocketlane_rejected", output=str(exc), decision="ESCALATE",
                           rationale="Non-retryable API error (payload/config)")
            self.escalator.escalate(run_id, email, "Rocketlane rejected the project request", str(exc),
                                    customer=deal.customer_name)
            return None
