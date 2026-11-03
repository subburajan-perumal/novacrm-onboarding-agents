"""Command line entry point: `uv run novacrm <command>`."""
from __future__ import annotations

import argparse
import os
import sys
import uuid

import yaml
from rich.console import Console
from rich.table import Table

from .audit import AuditLog
from .config import ROOT, load_config
from .escalation import Escalator
from .models import InboundEmail
from .orchestrator import Orchestrator
from .store import RunStore

console = Console()


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        console.print(f"[red]Missing {name} in .env[/red]")
        sys.exit(2)
    return val


def build(mail_mode: str = "gmail", echo: bool = True):
    cfg = load_config()
    from .adapters.llm import ClaudeLLM
    from .adapters.rocketlane import RocketlaneClient
    from .adapters.slack import SlackClient
    from .adapters.voice import RetellTwilioVoice
    from .agents.communication import CommunicationAgent
    from .agents.intake import IntakeAgent

    audit = AuditLog(ROOT / "logs" / "audit.jsonl", echo=echo)
    store = RunStore(ROOT / "data" / "runs.db")
    if mail_mode == "gmail":
        from .adapters.gmail import GmailClient
        mail = GmailClient(str(ROOT / os.environ.get("GMAIL_CREDENTIALS_FILE", "secrets/credentials.json")),
                           str(ROOT / os.environ.get("GMAIL_TOKEN_FILE", "secrets/token.json")))
    else:
        from .adapters.console_mail import ConsoleMail
        mail = ConsoleMail()
    llm = ClaudeLLM(cfg.settings["llm"]["model"], cfg.settings["llm"]["effort"],
                    fallbacks=cfg.settings["llm"].get("fallbacks", True))
    v = cfg.voice
    voice = RetellTwilioVoice(
        retell_api_key=_env("RETELL_API_KEY"), agent_id=_env("RETELL_AGENT_ID"),
        twilio_sid=_env("TWILIO_ACCOUNT_SID"), twilio_token=_env("TWILIO_AUTH_TOKEN"),
        from_number=_env("TWILIO_FROM_NUMBER"), ring_timeout=v["ring_timeout_seconds"],
        poll_seconds=v["call_poll_seconds"], max_wait_seconds=v["call_max_wait_seconds"],
    )
    rl = cfg.rocketlane
    projects = RocketlaneClient(_env("ROCKETLANE_API_KEY"), rl["base_url"], rl["max_retries"], rl["backoff_seconds"])
    chat = SlackClient(_env("SLACK_BOT_TOKEN"))
    escalator = Escalator(cfg, mail, audit, chat)
    intake = IntakeAgent(cfg, llm, voice, projects, store, audit, escalator)
    comm = CommunicationAgent(cfg, chat, llm, audit)
    return cfg, mail, Orchestrator(cfg, mail, intake, comm, store, audit, escalator), store, audit


def cmd_watch(args):
    cfg, _, orch, _, _ = build(args.mail)
    console.print(f"[bold]Intake Agent watching[/bold] {cfg.settings['cs_inbox']} every {cfg.gmail['poll_seconds']}s "
                  f"(query: {orch.inbox_query()})")
    orch.watch()


def cmd_once(args):
    _, _, orch, _, _ = build(args.mail)
    console.print(orch.poll_once() or "no new deal emails")


def cmd_run_sample(args):
    """Feed a sample email straight into the pipeline (no Gmail needed)."""
    samples = yaml.safe_load((ROOT / "samples" / "deals.yaml").read_text(encoding="utf-8"))
    s = samples[args.key]
    cfg, mail, orch, _, _ = build("console")
    email = InboundEmail(message_id="local-" + uuid.uuid4().hex[:8], thread_id="t-" + uuid.uuid4().hex[:8],
                         sender_email=cfg.aes[0].email, sender_name=cfg.aes[0].name,
                         to=cfg.settings["cs_inbox"], subject=s["subject"], body=s["body"])
    mail.inbox.append(email)
    orch.handle_email(email)


def cmd_send_sample(args):
    samples = yaml.safe_load((ROOT / "samples" / "deals.yaml").read_text(encoding="utf-8"))
    s = samples[args.key]
    cfg = load_config()
    from .adapters.gmail import GmailClient
    mail = GmailClient(str(ROOT / os.environ.get("GMAIL_CREDENTIALS_FILE", "secrets/credentials.json")),
                       str(ROOT / os.environ.get("GMAIL_TOKEN_FILE", "secrets/token.json")))
    mail.send(cfg.settings["cs_inbox"], s["subject"], s["body"])
    console.print(f"[green]Sent '{s['subject']}' to {cfg.settings['cs_inbox']}[/green]")


def cmd_resume(args):
    _, _, orch, _, _ = build(args.mail)
    console.print(orch.resume() or "nothing parked")


def cmd_runs(args):
    store = RunStore(ROOT / "data" / "runs.db")
    t = Table("run_id", "state", "customer", "tier", "project", "calls", "updated")
    for r in store.list():
        t.add_row(r["run_id"], r["state"], r["customer"] or "", r["tier"] or "", str(r["project_id"] or ""),
                  str(r["call_attempts"]), r["updated_at"][11:19])
    console.print(t)


def cmd_show(args):
    audit = AuditLog(ROOT / "logs" / "audit.jsonl", echo=False)
    t = Table("time", "agent", "step", "decision", "rationale", show_lines=False)
    for e in audit.entries(args.run_id):
        t.add_row(e["ts"][11:19], e["agent"], e["step"], e["decision"], e["rationale"][:90])
    console.print(t)


def cmd_setup_retell(args):
    cfg = load_config()  # loads .env
    from .adapters.voice import setup_retell_agent
    ids = setup_retell_agent(_env("RETELL_API_KEY"), voice_id=args.voice)
    console.print(f"[green]Created Retell agent.[/green] Add to .env:\nRETELL_AGENT_ID={ids['agent_id']}")


def cmd_call_test(args):
    cfg = load_config()
    from .adapters.voice import RetellTwilioVoice
    v = cfg.voice
    voice = RetellTwilioVoice(
        retell_api_key=_env("RETELL_API_KEY"), agent_id=_env("RETELL_AGENT_ID"),
        twilio_sid=_env("TWILIO_ACCOUNT_SID"), twilio_token=_env("TWILIO_AUTH_TOKEN"),
        from_number=_env("TWILIO_FROM_NUMBER"), ring_timeout=v["ring_timeout_seconds"],
    )
    out = voice.place_call(to_number=args.to or _env("AE_PHONE"), ae_name=cfg.aes[0].name,
                           customer_name="Acme Logistics", run_id="call-test", attempt=1)
    console.print(out.model_dump())


def cmd_check(args):
    """Smoke-test every credential without side effects."""
    load_config()
    import httpx
    results = []

    needs = {"Rocketlane": ["ROCKETLANE_API_KEY"], "Slack": ["SLACK_BOT_TOKEN"], "Retell": ["RETELL_API_KEY", "RETELL_AGENT_ID"],
             "Twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"], "Claude": ["ANTHROPIC_API_KEY"]}

    def check(name, fn):
        missing = [k for k in needs.get(name, []) if not os.environ.get(k, "").strip()]
        if missing:
            results.append((name, "SKIP", f"missing {', '.join(missing)}"))
            return
        try:
            results.append((name, "OK", str(fn())[:80]))
        except Exception as exc:
            results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"[:120]))

    check("Rocketlane", lambda: httpx.get("https://api.rocketlane.com/api/1.0/projects", params={"pageSize": 1},
                                           headers={"api-key": _env("ROCKETLANE_API_KEY")}, timeout=20).raise_for_status().status_code)
    check("Slack", lambda: httpx.post("https://slack.com/api/auth.test",
                                      headers={"Authorization": f"Bearer {_env('SLACK_BOT_TOKEN')}"}).json())
    check("Retell", lambda: httpx.get(f"https://api.retellai.com/get-agent/{_env('RETELL_AGENT_ID')}",
                                      headers={"Authorization": f"Bearer {_env('RETELL_API_KEY')}"},
                                      timeout=20).raise_for_status().json().get("agent_name"))

    def twilio():
        from twilio.rest import Client
        return Client(_env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")).api.accounts(_env("TWILIO_ACCOUNT_SID")).fetch().status
    check("Twilio", twilio)

    def claude():
        import anthropic
        r = anthropic.Anthropic().messages.create(model="claude-opus-5-5", max_tokens=50,
                                                  output_config={"effort": "low"},
                                                  messages=[{"role": "user", "content": "Reply with: ok"}])
        return r.stop_reason
    check("Claude", claude)
    t = Table("integration", "status", "detail")
    for row in results:
        t.add_row(*row)
    console.print(t)


def main(argv=None):
    p = argparse.ArgumentParser(prog="novacrm")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in [("watch", cmd_watch, "watch the CS inbox and process deal emails"),
                               ("once", cmd_once, "poll the inbox once"),
                               ("resume", cmd_resume, "retry parked runs")]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--mail", choices=["gmail", "console"], default="gmail")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("run-sample", help="push a sample email through the pipeline without Gmail")
    sp.add_argument("key")
    sp.set_defaults(fn=cmd_run_sample)
    sp = sub.add_parser("send-sample", help="email a sample deal notification to the CS inbox")
    sp.add_argument("key")
    sp.set_defaults(fn=cmd_send_sample)
    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs)
    sp = sub.add_parser("show", help="audit timeline for a run")
    sp.add_argument("run_id")
    sp.set_defaults(fn=cmd_show)
    sp = sub.add_parser("setup-retell", help="create the Retell voice agent")
    sp.add_argument("--voice", default="retell-Cimo")
    sp.set_defaults(fn=cmd_setup_retell)
    sp = sub.add_parser("call-test", help="place one tier-confirmation call")
    sp.add_argument("--to", default=None)
    sp.set_defaults(fn=cmd_call_test)
    sub.add_parser("check", help="verify all credentials").set_defaults(fn=cmd_check)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
