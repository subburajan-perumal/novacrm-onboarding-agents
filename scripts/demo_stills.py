"""Build the walkthrough's evidence stills from real screenshots, the real audit log and real pytest output.

Inputs: data/segments/raw/*, logs/audit.jsonl, data/pytest_verbose.txt
Output: data/segments/still_*.png (1920x888, rendered with headless Chrome)
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from demo_console import BASE_CSS, SLIDE, SLIDES  # noqa: E402

SEG = ROOT / "data" / "segments"
RAW = SEG / "raw"
WORK = ROOT / "data" / "stills_html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HELIOS_RUN = "run_13b7879b2c"
GROWTH_RUN = "run_eb3b549321"

CSS = BASE_CSS + """
body{padding:34px 44px;overflow:hidden}
h1{font-size:30px;margin:0 0 22px}h2{font-size:19px;margin:0 0 10px;color:var(--muted);font-weight:600}
.row{display:flex;gap:28px;align-items:flex-start}.col{display:flex;flex-direction:column;gap:22px;min-width:0}
.card{background:#fff;border-radius:14px;padding:14px;overflow:hidden}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.crop{overflow:hidden;border-radius:10px;background:#fff}.crop img{display:block}
img{max-width:100%}.note{color:var(--muted);font-size:17px;margin-top:14px;line-height:1.4}
table{border-collapse:collapse;font-size:18px;width:100%}td{padding:8px 10px;border-bottom:1px dashed var(--line);vertical-align:top;word-break:break-word}
td:first-child{color:var(--muted);white-space:nowrap;width:150px}.warn{color:#fde68a;font-weight:600}
pre{margin:0;font:14px/1.42 Consolas,monospace;color:#cbd5e1;white-space:pre-wrap}.cols{column-count:2;column-gap:40px}.ok{color:#86efac}
.line{font-size:21px;margin:14px 0;line-height:1.4}.agent{color:#a5b4fc}.user{color:#86efac}
.e{display:grid;grid-template-columns:90px 130px 1fr;gap:10px;padding:5px 4px;border-bottom:1px dashed #1c2748;font:15px/1.35 Consolas,monospace}
.t{color:var(--muted)}.a{font-weight:700}.intake{color:var(--intake)}.communication{color:var(--comm)}.orchestrator{color:var(--orch)}.guardrail{color:var(--guard)}
.d{display:inline-block;margin-left:8px;padding:0 8px;border-radius:999px;font-size:12px;font-weight:700;background:#14532d;color:#bbf7d0}
.d.MOCKED{background:#78350f;color:#fde68a}.r{color:var(--muted);font-size:13px}
"""


def crop(name: str, x: int, y: int, w: int, h: int, width: int) -> str:
    """Show the (x, y, w, h) region of a raw screenshot, scaled to `width` pixels."""
    scale = width / w
    full_w = {"auto_fired.jpg": 1568, "email_helios.png": 1064, "growth_oncall.png": 1864}.get(name, 1540)
    return (f'<div class="crop" style="width:{width}px;height:{round(h * scale)}px">'
            f'<img src="{(RAW / name).as_uri()}" style="width:{round(full_w * scale)}px;max-width:none;'
            f'margin-left:{-round(x * scale)}px;margin-top:{-round(y * scale)}px"></div>')


def page(body: str) -> str:
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{body}</body></html>'


def audit(run_id: str) -> list[dict]:
    rows = [json.loads(x) for x in (ROOT / "logs" / "audit.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return [e for e in rows if e["run_id"] == run_id]


def still_growth_call() -> str:
    transcript = next(e for e in audit(GROWTH_RUN) if e["step"] == "voice_call_result")["output"]["transcript"]
    lines = ""
    for line in transcript.strip().splitlines():
        who, _, said = line.partition(":")
        cls, label = ("agent", "Nova (agent)") if who == "Agent" else ("user", "AE (real person)")
        lines += f'<div class="line {cls}"><b>{label}:</b> {html.escape(said.strip())}</div>'
    return page('<h1>Growth run, live: a real person answers as the AE</h1><div class="row">'
                f'<div class="col" style="width:620px">{crop("growth_oncall.png", 680, 300, 500, 350, 620)}'
                '<div class="note">Retell web call, not a phone line. The audio you hear and this transcript '
                'are from Retell\'s record of the call.</div></div>'
                f'<div class="panel" style="flex:1"><h2>Transcript (from Retell)</h2>{lines}</div></div>')


def still_growth_log() -> str:
    items = ""
    for e in audit(GROWTH_RUN):
        badge = f'<span class="d {html.escape(e["decision"])}">{html.escape(e["decision"])}</span>' if e["decision"] else ""
        items += (f'<div class="e"><span class="t">{e["ts"][11:19]}</span><span class="a {e["agent"]}">{e["agent"]}</span>'
                  f'<span>{e["step"].replace("_", " ")}{badge}<div class="r">{html.escape(e["rationale"])}</div></span></div>')
    return page(f'<h1>Growth run: the agents\' audit log</h1><div class="panel">{items}</div>')


def still_email() -> str:
    out = next(e for e in audit(HELIOS_RUN) if e["step"] == "extracted_fields")["output"]
    labels = [("customer_name", "Customer"), ("customer_contact_name", "Contact"), ("customer_contact_email", "Contact email"),
              ("ae_name", "AE"), ("salesforce_opportunity_url", "Salesforce"), ("close_date", "Close date"), ("notes", "Notes")]
    table = "".join(f"<tr><td>{label}</td><td>{html.escape(str(out[k]['value']))}</td></tr>" for k, label in labels)
    table += '<tr><td>Plan tier</td><td class="warn">Not in the email, so the agent must call the AE</td></tr>'
    return page(f'<h1>The AE\'s deal email, and what the Intake Agent extracts from it</h1><div class="row">'
                f'<div class="card">{crop("email_helios.png", 0, 0, 560, 351, 1000)}</div>'
                f'<div class="panel" style="flex:1"><h2>Claude extraction (from the audit log)</h2><table>{table}</table></div></div>')


def still_reply() -> str:
    return page('<h1>The agent\'s reply on the AE\'s thread</h1>'
                f'<div class="card" style="width:1500px;margin:0 auto"><img src="{(RAW / "email_reply.png").as_uri()}" style="width:100%"></div>')


def still_recipients() -> str:
    block1 = crop("auto_block1.jpg", 782, 100, 751, 215, 880)
    block2 = crop("auto_block2.jpg", 782, 100, 751, 215, 880)
    fired = crop("auto_fired.jpg", 47, 150, 774, 125, 880)
    return page('<h1>Overdue task escalation: who gets notified, and proof it fired</h1><div class="row">'
                f'<div class="col" style="width:880px"><div><h2>Block 1: overdue by 1 day → Project Manager</h2>{block1}</div>'
                f'<div><h2>Block 2: overdue by 4 days → Project Owner</h2>{block2}</div></div>'
                f'<div class="col" style="width:880px"><div><h2>Fired for real: the Project Owner\'s Rocketlane notification</h2>{fired}</div>'
                '<div class="note">Block 1 also lists a stray "undefined" entry that would not delete in the Rocketlane editor. '
                'The Project Manager entries are the intended recipients. I have seen the 4-day block fire; the 1-day block '
                'notifies a sample user account, so I have not seen that notification arrive.</div></div></div>')


def still_tests() -> str:
    text = (ROOT / "data" / "pytest_verbose.txt").read_text(encoding="utf-8-sig", errors="replace")
    names = re.findall(r"tests/(test_\w+\.py::\S+) PASSED", text)
    summary = next(ln for ln in reversed(text.splitlines()) if " passed" in ln).strip("= \t")
    body = "\n".join(f"{html.escape(n.split('.py::')[0] + ': ' + n.split('.py::')[1])} ✓" for n in names)
    return page(f'<h1>Automated tests: <span class="ok">{html.escape(summary)}</span></h1><pre class="cols">{body}</pre>')


def still_outro() -> str:
    return SLIDE.replace("__BODY__", SLIDES["outro"])


def render(name: str, doc: str) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    src = WORK / f"{name}.html"
    src.write_text(doc, encoding="utf-8")
    out = SEG / f"{name}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={WORK / 'profile'}",
                    "--allow-file-access-from-files", "--window-size=1920,888", "--force-device-scale-factor=1",
                    "--virtual-time-budget=2000", f"--screenshot={out}", src.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(out.name, out.stat().st_size)


if __name__ == "__main__":
    for key, fn in {"still_email": still_email, "still_reply": still_reply, "still_recipients": still_recipients,
                    "still_tests": still_tests, "still_outro": still_outro, "still_growth_call": still_growth_call,
                    "still_growth_log": still_growth_log}.items():
        render(key, fn())
