"""Rocketlane REST API client (https://developer.rocketlane.com) with retries for transient failures."""
from __future__ import annotations

import time
from typing import Callable, Optional

import httpx


class RocketlaneError(Exception):
    """Non-retryable API error (4xx): bad payload, auth, not found."""


class RocketlaneUnavailable(Exception):
    """Rocketlane is down / timing out after all retries."""


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").lower().split())


class RocketlaneClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.rocketlane.com/api/1.0",
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"api-key": api_key, "accept": "application/json", "content-type": "application/json"},
            timeout=60,
            transport=transport,
        )
        self.create_timeout = 120  # project creation with a template import can take a while
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self.sleep = sleep
        self.attempt_log: list[str] = []

    def _request(self, method: str, path: str, **kw) -> dict:
        last = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.http.request(method, path, **kw)
            except httpx.TransportError as exc:  # timeouts, DNS, connection refused
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp.json() if resp.content else {}
                if resp.status_code >= 500 or resp.status_code == 429:
                    last = f"HTTP {resp.status_code}"
                else:
                    raise RocketlaneError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            self.attempt_log.append(f"{method} {path} attempt {attempt}: {last}")
            if attempt < self.max_retries:
                self.sleep(self.backoff * 2 ** (attempt - 1))
        raise RocketlaneUnavailable(f"{method} {path} failed after {self.max_retries} attempts ({last})")

    # ---- reads -------------------------------------------------------------
    def _paged(self, path: str, params: dict) -> list[dict]:
        out, params = [], {**params, "pageSize": 100}
        while True:
            body = self._request("GET", path, params=params)
            out.extend(body.get("data", []))
            pag = body.get("pagination") or {}
            if not pag.get("hasMore") or not pag.get("nextPageToken"):
                return out
            params = {"pageToken": pag["nextPageToken"], "pageSize": 100}

    def find_existing_projects(self, customer_name: str, opportunity_id: str) -> list[dict]:
        """Active projects for this customer (by company or project name). Each result carries
        `opportunity_match`: True if one of its field values holds this Salesforce opportunity id."""
        hits = []
        for p in self._paged("/projects", {"projectName.cn": customer_name, "includeAllFields": "true"}):
            company = (p.get("customer") or {}).get("companyName")
            if p.get("archived"):
                continue
            if _norm(company) == _norm(customer_name) or _norm(customer_name) in _norm(p.get("projectName")):
                values = " ".join(str(f.get("fieldValue", "")) for f in p.get("fields") or [])
                hits.append({**p, "opportunity_match": bool(opportunity_id) and opportunity_id in values})
        return hits

    def find_user_id(self, email: str) -> Optional[int]:
        if not hasattr(self, "_users"):
            self._users = {(u.get("email") or "").lower(): u["userId"] for u in self._paged("/users", {})}
        return self._users.get(email.lower())

    def list_tasks(self, project_id: int) -> list[dict]:
        return self._paged("/tasks", {"projectId.eq": project_id, "includeAllFields": "true"})

    def get_project(self, project_id: int) -> dict:
        return self._request("GET", f"/projects/{project_id}")

    # ---- writes ------------------------------------------------------------
    def _find_created(self, payload: dict) -> Optional[dict]:
        for p in self._paged("/projects", {"projectName.eq": payload["projectName"]}):
            if (p.get("customer") or {}).get("companyName") == payload["customer"]["companyName"] and not p.get("archived"):
                return p
        return None

    def create_project(self, payload: dict) -> dict:
        """POST is not idempotent: a timed-out request may still have created the project
        (template import can be slow). Before every retry, look for it instead of re-posting."""
        last = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.http.post("/projects", json=payload, timeout=self.create_timeout)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code < 500 and resp.status_code != 429:
                    raise RocketlaneError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                last = f"HTTP {resp.status_code}"
            self.attempt_log.append(f"POST /projects attempt {attempt}: {last}")
            self.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                found = self._find_created(payload)
            except RocketlaneUnavailable:
                found = None
            if found:
                self.attempt_log.append(f"POST /projects: found project {found.get('projectId')} created by the failed request")
                return found
        raise RocketlaneUnavailable(f"POST /projects failed after {self.max_retries} attempts ({last})")

    def import_template(self, project_id: int, template_id: int, start_date: str) -> dict:
        return self._request(
            "POST", f"/projects/{project_id}/import-template", json=[{"templateId": template_id, "startDate": start_date}]
        )
