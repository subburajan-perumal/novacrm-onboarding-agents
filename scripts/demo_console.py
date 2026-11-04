"""Local demo console for the video walkthrough (http://localhost:8767).

  /            live view: caption bar + the agents' audit log as it happens + command output panel
  /slide/<n>   title, architecture and outro slides
Captions come from data/demo_caption.txt, command output from data/demo_terminal.txt.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "logs" / "audit.jsonl"
CAPTION = ROOT / "data" / "demo_caption.txt"
TERMINAL = ROOT / "data" / "demo_terminal.txt"
START_LINE = len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0

BASE_CSS = """
:root{--bg:#0b1020;--panel:#121a30;--line:#22305a;--fg:#e8ecf7;--muted:#8b95b2;--accent:#7c8cff;
--intake:#38bdf8;--comm:#e879f9;--orch:#facc15;--guard:#fb7185;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font:18px/1.45 "Segoe UI",system-ui,sans-serif}
"""

LIVE = """<!doctype html><html><head><meta charset="utf-8"><title>NovaCRM agents · live</title><style>""" + BASE_CSS + """
header{padding:18px 28px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,#6366f1,#22d3ee);display:grid;place-items:center;font-weight:800}
#cap{font-size:26px;font-weight:600}
main{display:grid;grid-template-columns:1.55fr 1fr;gap:18px;padding:18px 28px;height:calc(100% - 80px)}
section{background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden;display:flex;flex-direction:column}
h2{margin:0;padding:12px 18px;font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line)}
#log{overflow:auto;padding:10px 14px;font:15px/1.5 Consolas,"Cascadia Mono",monospace}
.e{display:grid;grid-template-columns:78px 128px 1fr;gap:10px;padding:6px 4px;border-bottom:1px dashed #1c2748}
.t{color:var(--muted)}.a{font-weight:700}.intake{color:var(--intake)}.communication{color:var(--comm)}
.orchestrator{color:var(--orch)}.guardrail{color:var(--guard)}
.d{display:inline-block;margin-left:8px;padding:0 8px;border-radius:999px;font-size:12px;font-weight:700;background:#1e293b}
.d.PASS,.d.ACCEPT,.d.CREATED,.d.DONE,.d.INVITED{background:#14532d;color:#bbf7d0}
.d.FAIL,.d.ESCALATE,.d.DUPLICATE{background:#7f1d1d;color:#fecaca}
.d.RETRY,.d.ASK_AE,.d.MOCKED,.d.FALLBACK,.d.WARN,.d.PARK,.d.CALL_ANYWAY{background:#78350f;color:#fde68a}
.r{color:var(--muted);font-size:13px;margin-top:2px}
#term{margin:0;padding:14px 16px;overflow:auto;font:14px/1.45 Consolas,monospace;color:#cbd5e1;white-space:pre-wrap}
</style></head><body>
<header><div class="logo">N</div><div id="cap">NovaCRM onboarding agents</div></header>
<main><section><h2>Agent activity · live audit log</h2><div id="log"></div></section>
<section><h2>Details</h2><pre id="term"></pre></section></main>
<script>
let since = 0;
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function tick() {
  try {
    const r = await (await fetch("/events?since=" + since)).json();
    since = r.next;
    document.getElementById("cap").textContent = r.caption || "NovaCRM onboarding agents";
    document.getElementById("term").textContent = r.terminal || "";
    const log = document.getElementById("log");
    for (const e of r.entries) {
      const div = document.createElement("div"); div.className = "e";
      const dec = e.decision ? `<span class="d ${esc(e.decision)}">${esc(e.decision)}</span>` : "";
      div.innerHTML = `<span class="t">${esc(e.ts.slice(11,19))}</span><span class="a ${esc(e.agent)}">${esc(e.agent)}</span>
        <span>${esc(e.step.replaceAll("_"," "))}${dec}${e.rationale ? `<div class="r">${esc(e.rationale)}</div>` : ""}</span>`;
      log.appendChild(div); log.scrollTop = 1e9;
    }
  } catch (_) {}
}
setInterval(tick, 700); tick();
</script></body></html>"""

SLIDE = """<!doctype html><html><head><meta charset="utf-8"><title>NovaCRM agents</title><style>""" + BASE_CSS + """
body{display:grid;place-items:center}.s{width:min(1500px,92vw)}
h1{font-size:54px;line-height:1.1;margin:0 0 10px}.sub{font-size:24px;color:var(--muted);margin-bottom:34px}
ul{font-size:26px;margin:0;padding-left:28px}li{margin:12px 0}b{color:var(--accent)}
.flow{display:flex;gap:14px;align-items:stretch;margin:26px 0;flex-wrap:wrap}
.box{flex:1;min-width:200px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px 18px;font-size:19px}
.box h3{margin:0 0 6px;font-size:21px}.arrow{align-self:center;font-size:30px;color:var(--muted)}
.note{margin-top:26px;font-size:19px;color:var(--muted)}
</style></head><body><div class="s">__BODY__</div></body></html>"""

SLIDES = {
    "title": """<h1>NovaCRM customer onboarding<br>multi-agent automation</h1>
<div class="sub">Rocketlane Forward Deployed Engineer take-home · Subburajan Perumal</div>
<ul><li><b>Intake &amp; Routing Agent</b>: Gmail deal email → Claude extraction → voice call to the AE → Rocketlane project from the right template</li>
<li><b>Communication Agent</b>: personalized Slack onboarding channel</li>
<li><b>Rocketlane automation</b>: overdue task escalation (1 day → Project Manager, 4 days → Project Owner)</li>
<li><b>Guardrails</b>: no guessed fields, no project without a confirmed tier, retries + human escalation, full audit log</li></ul>""",
    "arch": """<h1>How it works</h1><div class="flow">
<div class="box"><h3>1 · Intake</h3>Watches the CS inbox. Claude extracts fields with verbatim evidence. Rules reject anything missing or not in the email.</div><div class="arrow">→</div>
<div class="box"><h3>2 · Voice</h3>Retell voice agent asks the AE: Enterprise or Growth? Reads it back. Accepted only if confirmed AND an independent transcript check agrees.</div><div class="arrow">→</div>
<div class="box"><h3>3 · Rocketlane</h3>Duplicate check, then project from the tier's template (30 / 14 days), dedicated or pooled CSM as Project Manager. Task set verified.</div><div class="arrow">→</div>
<div class="box"><h3>4 · Slack</h3>Communication Agent: channel, topic and welcome message per tier, checked for the wrong tier's terms.</div></div>
<ul><li>No answer → retry (3 calls) · unclear → re-call · then <b>escalate to the CS manager</b> (email + #cs-escalations). Never a default tier.</li>
<li>Rocketlane down → retries with backoff → run parked and resumable. Every step audited with input, output, decision and rationale.</li></ul>
<div class="note">Demo transport: the AE answers on an "incoming call" page and the AE's voice is a scripted text-to-speech stand-in so runs are reproducible.
The conversation with the Retell agent is real. Production rings the AE's mobile through a Twilio number (same agent, one config switch).</div>""",
    "outro": """<h1>What changes in production</h1><ul>
<li>Twilio (or local carrier) number rings the AE's mobile; business-hours calling window</li>
<li>Gmail push (Pub/Sub watch) and Retell/Twilio webhooks instead of polling</li>
<li>Slack Connect invite to the customer (paid Slack plan); here it is logged as mocked</li>
<li>Salesforce API for AE phone and a tier cross-check; Postgres + tracing instead of SQLite/JSONL</li>
<li>Secrets manager, scheduled resume of parked runs, alerting</li></ul>
<div class="note">42 automated tests: happy path, validation, template accuracy, Rocketlane down, existing project, no answer, ambiguous tier, idempotency.</div>""",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/events"):
            since = int(self.path.split("since=")[-1] or 0) if "since=" in self.path else 0
            lines = AUDIT.read_text(encoding="utf-8").splitlines() if AUDIT.exists() else []
            start = max(since, START_LINE)
            entries = [json.loads(x) for x in lines[start:] if x.strip()]
            cap = CAPTION.read_text(encoding="utf-8").strip() if CAPTION.exists() else ""
            term = TERMINAL.read_text(encoding="utf-8") if TERMINAL.exists() else ""
            if not term.strip():
                calls = [json.loads(x) for x in lines[START_LINE:] if '"voice_call_result"' in x]
                if calls:
                    o = calls[-1]["output"] or {}
                    term = (f"Last call · {o.get('status')} · voice tier: {o.get('voice_tier')} · "
                            f"read-back confirmed: {o.get('readback_confirmed')}\n"
                            f"Decision: {calls[-1]['decision']} — {calls[-1]['rationale']}\n\n"
                            f"Transcript (from Retell):\n{o.get('transcript') or '(no conversation)'}")
            return self._send(json.dumps({"next": len(lines), "entries": entries, "caption": cap, "terminal": term}),
                              "application/json")
        if self.path.startswith("/slide/"):
            name = self.path.split("/")[-1]
            return self._send(SLIDE.replace("__BODY__", SLIDES.get(name, "<h1>?</h1>")))
        return self._send(LIVE)


if __name__ == "__main__":
    for f in (CAPTION, TERMINAL):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="utf-8")
    ThreadingHTTPServer(("127.0.0.1", 8767), Handler).serve_forever()
