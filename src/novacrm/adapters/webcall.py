"""Demo telephony: the AE "phone" is a local incoming-call page; the conversation is a real Retell
voice call (WebRTC) with the same agent, prompt and post-call analysis as the PSTN path.

place_call() -> opens http://localhost:8766/ring/<id> ("Incoming call from NovaCRM Onboarding").
  Answer  -> server creates a Retell v3 web call, browser joins it, live transcript shown.
  Decline -> no_answer (user_declined).   No action within ring_timeout -> no_answer (missed).
Then the Retell call record is polled until ended + analyzed, exactly like the phone path.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import httpx

from ..models import CallOutcome
from .voice import RETELL_BASE, interpret_call

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Incoming call</title>
<style>
:root{--bg:#0b1020;--card:#131a2e;--fg:#e8ecf7;--muted:#8b95b2;--ok:#22c55e;--no:#ef4444}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.card{width:min(420px,92vw);background:var(--card);border-radius:24px;padding:32px 28px;text-align:center;box-shadow:0 20px 60px #0008}
.avatar{width:88px;height:88px;border-radius:50%;margin:0 auto 14px;display:grid;place-items:center;font-size:34px;font-weight:700;
background:linear-gradient(135deg,#6366f1,#22d3ee)}
.ringing .avatar{animation:pulse 1.2s infinite}@keyframes pulse{0%{box-shadow:0 0 0 0 #6366f199}70%{box-shadow:0 0 0 22px #6366f100}100%{box-shadow:0 0 0 0 #6366f100}}
h1{font-size:22px;margin:0}.sub{color:var(--muted);margin:4px 0 22px}
.btns{display:flex;gap:18px;justify-content:center}button{border:0;border-radius:999px;padding:14px 26px;font-size:16px;font-weight:600;color:#fff;cursor:pointer}
.ok{background:var(--ok)}.no{background:var(--no)}#status{margin-top:18px;color:var(--muted);min-height:1.4em}
#tx{margin-top:16px;text-align:left;max-height:240px;overflow:auto;font-size:14px}#tx p{margin:6px 0}.agent{color:#a5b4fc}.user{color:#86efac}
</style></head><body><div class="card ringing" id="card">
<div class="avatar">N</div><h1>Nova · NovaCRM Onboarding</h1>
<div class="sub">Incoming call · about the <b>__CUSTOMER__</b> deal</div>
<div class="btns" id="btns"><button class="no" id="decline">Decline</button><button class="ok" id="answer">Answer</button></div>
<div id="status">Ringing…</div><div id="tx"></div></div>
<script type="module">
import { RetellWebClient } from "https://cdn.jsdelivr.net/npm/retell-client-js-sdk@3.0.3/+esm";
const id = "__RING_ID__", $ = s => document.querySelector(s), card = $("#card");
const setStatus = t => $("#status").textContent = t;
const done = t => { card.classList.remove("ringing"); $("#btns").style.display = "none"; setStatus(t); };
const poll = setInterval(async () => {           // missed-call timeout is decided by the agent
  const s = await (await fetch(`/ring/${id}/state`)).json();
  if (s.state === "missed") { clearInterval(poll); done("Missed call"); }
}, 1000);
$("#decline").onclick = async () => { clearInterval(poll); await fetch(`/ring/${id}/decline`, {method: "POST"}); done("Call declined"); };
$("#answer").onclick = async () => {
  clearInterval(poll); card.classList.remove("ringing"); $("#btns").style.display = "none"; setStatus("Connecting…");
  const r = await fetch(`/ring/${id}/answer`, {method: "POST"});
  if (!r.ok) { setStatus("Call could not be connected"); return; }
  const c = await r.json();
  const client = new RetellWebClient();
  client.on("call_started", () => setStatus("On call · speak normally"));
  client.on("call_ended", () => setStatus("Call ended. You can close this tab."));
  client.on("error", e => { setStatus("Call error: " + (e && e.message || e)); client.stopCall && client.stopCall(); });
  client.on("update", u => {
    if (!u || !u.transcript) return;
    $("#tx").innerHTML = u.transcript.map(t => `<p class="${t.role}"><b>${t.role === "agent" ? "Nova" : "You"}:</b> ${t.content}</p>`).join("");
    $("#tx").scrollTop = 1e9;
  });
  await client.startCall({ callId: c.call_id, accessToken: c.access_token, transport: c.transport, iceServers: c.ice_servers });
};
</script></body></html>"""


class _Ring:
    def __init__(self, customer: str, payload: dict):
        self.customer = customer
        self.payload = payload
        self.state = "ringing"          # ringing | answered | declined | missed
        self.call: dict | None = None
        self.lock = threading.Lock()


class RetellWebCallVoice:
    def __init__(self, *, retell_api_key: str, agent_id: str, ring_timeout: int = 25, poll_seconds: float = 3,
                 max_wait_seconds: int = 300, port: int = 8766, open_browser: bool = True,
                 sleep: Callable[[float], None] = time.sleep):
        self.retell = httpx.Client(base_url=RETELL_BASE, headers={"Authorization": f"Bearer {retell_api_key}"}, timeout=30)
        self.agent_id = agent_id
        self.ring_timeout, self.poll, self.max_wait = ring_timeout, poll_seconds, max_wait_seconds
        self.port, self.open_browser, self.sleep = port, open_browser, sleep
        self.rings: dict[str, _Ring] = {}
        self._server: ThreadingHTTPServer | None = None

    # ---- local "phone" ---------------------------------------------------------
    def _ensure_server(self) -> None:
        if self._server:
            return
        voice = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the agent console clean
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _ring(self) -> tuple[str, _Ring | None, str]:
                parts = self.path.strip("/").split("/")
                rid = parts[1] if len(parts) > 1 else ""
                return rid, voice.rings.get(rid), parts[2] if len(parts) > 2 else ""

            def do_GET(self):
                rid, ring, action = self._ring()
                if ring is None:
                    return self._send(404, b'{"error":"unknown call"}')
                if action == "state":
                    return self._send(200, json.dumps({"state": ring.state}).encode())
                html = PAGE.replace("__RING_ID__", rid).replace("__CUSTOMER__", ring.customer)
                return self._send(200, html.encode(), "text/html; charset=utf-8")

            def do_POST(self):
                rid, ring, action = self._ring()
                if ring is None:
                    return self._send(404, b'{"error":"unknown call"}')
                with ring.lock:
                    if action == "decline" and ring.state == "ringing":
                        ring.state = "declined"
                        return self._send(200, b"{}")
                    if action == "answer" and ring.state == "ringing":
                        resp = voice.retell.post("/v3/create-web-call", json=ring.payload)
                        if resp.status_code >= 400:
                            ring.state = "declined"
                            return self._send(502, resp.content)
                        ring.call = resp.json()
                        ring.state = "answered"
                        return self._send(200, json.dumps(ring.call).encode())
                return self._send(409, json.dumps({"state": ring.state}).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # ---- VoiceClient -----------------------------------------------------------
    def place_call(self, *, to_number: str, ae_name: str, customer_name: str, run_id: str, attempt: int) -> CallOutcome:
        self._ensure_server()
        rid = uuid.uuid4().hex[:12]
        ring = _Ring(customer_name, {
            "agent_id": self.agent_id,
            "metadata": {"run_id": run_id, "attempt": attempt, "transport": "web-demo"},
            "retell_llm_dynamic_variables": {"ae_name": ae_name.split()[0], "customer_name": customer_name},
        })
        self.rings[rid] = ring
        url = f"http://localhost:{self.port}/ring/{rid}"
        if self.open_browser:
            webbrowser.open(url, new=2)

        deadline = time.monotonic() + self.ring_timeout
        while time.monotonic() < deadline and ring.state == "ringing":
            self.sleep(0.5)
        with ring.lock:
            if ring.state == "ringing":
                ring.state = "missed"
        if ring.state == "missed":
            return CallOutcome(status="no_answer", raw_reason="web:missed (not answered in time)")
        if ring.state == "declined":
            return CallOutcome(status="no_answer", raw_reason="web:user_declined")

        call_id = ring.call["call_id"]
        end = time.monotonic() + self.max_wait
        call: dict = {}
        while time.monotonic() < end:
            r = self.retell.get(f"/v2/get-call/{call_id}")
            r.raise_for_status()
            call = r.json()
            status = call.get("call_status")
            if status == "error" or (status == "ended" and call.get("call_analysis") is not None):
                break
            self.sleep(self.poll)
        return interpret_call(call_id, call)
