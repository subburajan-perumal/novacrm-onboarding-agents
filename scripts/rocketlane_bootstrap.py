"""One-time Rocketlane workspace setup through the API.

  uv run python scripts/rocketlane_bootstrap.py fields       # custom project fields
  uv run python scripts/rocketlane_bootstrap.py blueprints   # template-source projects (then "Save as template" in UI)
  uv run python scripts/rocketlane_bootstrap.py seed-overdue # demo project with tasks 2 and 5 days overdue
  uv run python scripts/rocketlane_bootstrap.py templates    # list project templates and their IDs
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
BASE = "https://api.rocketlane.com/api/1.0"
H = {"api-key": os.environ["ROCKETLANE_API_KEY"], "accept": "application/json", "content-type": "application/json"}
OWNER = "subburajanperumal@gmail.com"


def call(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, BASE + path, headers=H, timeout=30, **kw)
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}


def fields() -> None:
    existing = {f["fieldLabel"]: f["fieldId"] for f in
                call("GET", "/fields", params={"pageSize": 100, "objectType.eq": "PROJECT"}).get("data", [])}
    wanted = [
        {"fieldLabel": "Plan Tier", "fieldType": "TEXT", "fieldDescription": "Enterprise or Growth, confirmed by the AE on a call"},
        {"fieldLabel": "CSM Model", "fieldType": "TEXT", "fieldDescription": "Dedicated (Enterprise) or Pooled (Growth)"},
        {"fieldLabel": "Salesforce Opportunity", "fieldType": "TEXT"},
        {"fieldLabel": "Account Executive", "fieldType": "TEXT"},
        {"fieldLabel": "Project Manager", "fieldType": "SINGLE_USER", "fieldDescription": "Assigned CSM; notified when a task is 1 day overdue"},
    ]
    for f in wanted:
        if f["fieldLabel"] in existing:
            print(f"exists  {f['fieldLabel']}: {existing[f['fieldLabel']]}")
            continue
        made = call("POST", "/fields", json={**f, "objectType": "PROJECT", "enabled": True, "private": False})
        print(f"created {f['fieldLabel']}: {made.get('fieldId')}")


def _project(name: str, company: str, start: date, due: date, fields_: list | None = None) -> int:
    body = {"projectName": name, "owner": {"emailId": OWNER}, "customer": {"companyName": company},
            "autoCreateCompany": True, "startDate": start.isoformat(), "dueDate": due.isoformat()}
    if fields_:
        body["fields"] = fields_
    p = call("POST", "/projects", json=body)
    print(f"project {p['projectId']}: {name}")
    return p["projectId"]


def _phase(project_id: int, name: str, start: date, due: date) -> int:
    ph = call("POST", "/phases", json={"phaseName": name, "project": {"projectId": project_id},
                                        "startDate": start.isoformat(), "dueDate": due.isoformat()})
    return ph["phaseId"]


def _task(project_id: int, phase_id: int, name: str, start: date, due: date, ttype: str = "TASK",
          description: str | None = None, assignee: str | None = None) -> int:
    body = {"taskName": name, "project": {"projectId": project_id}, "phase": {"phaseId": phase_id},
            "startDate": start.isoformat(), "dueDate": due.isoformat(), "type": ttype}
    if description:
        body["taskDescription"] = f"<p>{description}</p>"
    if assignee:
        body["assignees"] = {"members": [{"emailId": assignee}]}
    return call("POST", "/tasks", json=body)["taskId"]


def blueprints() -> None:
    tpl = yaml.safe_load((ROOT / "config" / "onboarding_template.yaml").read_text(encoding="utf-8"))
    start = date(2026, 11, 2)  # a Monday; templates are relative, so the date doesn't matter
    for tier, days in (("enterprise", 30), ("growth", 14)):
        label = "Enterprise" if tier == "enterprise" else "Growth"
        pid = _project(f"TEMPLATE SOURCE - NovaCRM Onboarding - {label} ({days} days)", "NovaCRM Templates",
                       start, start + timedelta(days=days - 1))
        n = 0
        for ph in tpl["phases"]:
            a, b = ph[tier]
            phase_id = _phase(pid, ph["name"], start + timedelta(days=a - 1), start + timedelta(days=b - 1))
            for t in ph["tasks"]:
                ta, tb = t[tier]
                _task(pid, phase_id, t["name"], start + timedelta(days=ta - 1), start + timedelta(days=tb - 1),
                      t.get("type", "TASK"), t.get("description"))
                n += 1
        print(f"  {label}: {len(tpl['phases'])} phases, {n} tasks")


def seed_overdue() -> None:
    today = date.today()
    pid = _project("Globex Corp - Onboarding (Enterprise) [overdue demo]", "Globex Corp",
                   today - timedelta(days=10), today + timedelta(days=19))
    ph = _phase(pid, "Data Migration", today - timedelta(days=10), today + timedelta(days=5))
    _task(pid, ph, "Field mapping approved by customer (2 days overdue)", today - timedelta(days=6),
          today - timedelta(days=2), assignee=OWNER)
    _task(pid, ph, "Data export received from customer (5 days overdue)", today - timedelta(days=9),
          today - timedelta(days=5), assignee=OWNER)
    print(f"seeded overdue demo project {pid}")


def templates() -> None:
    for t in call("GET", "/templates").get("data", []):
        if t.get("objectType") == "PROJECT":
            print(t["templateId"], t["templateName"])


if __name__ == "__main__":
    {"fields": fields, "blueprints": blueprints, "seed-overdue": seed_overdue,
     "templates": templates}[sys.argv[1]]()
