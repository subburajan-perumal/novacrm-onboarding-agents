"""Narration script for the recorded walkthrough + helpers used while recording.

  python scripts/demo_narration.py tts          # render narration WAVs (Windows SAPI, Microsoft David)
  python scripts/demo_narration.py cap <id>     # show caption <id> on the console + log its timestamp
  python scripts/demo_narration.py cut start|end  # mark a dead-time interval to remove in post
  python scripts/demo_narration.py dur <id>     # print narration length (s), to pace the recording
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NARR = DATA / "narration"
TIMELINE = DATA / "demo_timeline.jsonl"
CAPTION = DATA / "demo_caption.txt"

LINES = {
    "c01": "NovaCRM customer onboarding, automated with a multi agent system. This is my Rocketlane Forward Deployed Engineer take-home.",
    "c02": "An Intake agent reads the deal email, confirms the plan tier with the account executive by voice, and creates the Rocketlane project. A Communication agent then sets up the customer's Slack channel. Every decision passes through deterministic guardrails.",
    "p01": "Positive run. The account executive emails the customer success inbox about a new deal, Helios Freight. On the right are the fields Claude extracts from it. The plan tier is not in the email.",
    "c03": "The email arrives, and the agent starts a new run.",
    "c04": "The Intake agent picks it up. Claude extracts each field with verbatim evidence, and the guardrail confirms that everything required is present.",
    "c05": "The plan tier is not in the email, so the agent calls the account executive. This is a Retell web call on an incoming call page, not a phone line. In this run, the A E's voice is a scripted stand-in. The conversation with the Retell voice agent is real.",
    "c06": "The A E says Enterprise and confirms the read-back. The tier is accepted only because an independent transcript check agrees.",
    "c07": "The Rocketlane project is created from the thirty day Enterprise template. Four phases, fifteen tasks, and a dedicated C S M set as Project Manager.",
    "c08": "The Communication agent creates a personalized Slack channel, with a tier specific name, topic and welcome message. The customer's Slack Connect invite is mocked on the free plan.",
    "c09": "Overdue escalation is a native Rocketlane automation. One day overdue notifies the Project Manager. Four days overdue notifies the Project Owner.",
    "a01": "Here are the recipients: the Project Manager at one day, and the Project Owner at four days. And it has really fired. This is the four day escalation reaching the Project Owner on a seeded demo task.",
    "g01": "Now the Growth path, live. This time the A E on the call is a real person, me, in my own voice, on the same web call.",
    "g02": "The A E says Growth and confirms it. So the agent picks the fourteen day template and the pooled C S M.",
    "g03": "The Growth project in Rocketlane: the fourteen day template, fifteen tasks, and the pooled C S M.",
    "g04": "And the Slack channel uses the Growth name, topic and welcome message.",
    "c10": "Negative run one. This email has a malformed contact address, and the link is not a Salesforce opportunity.",
    "c11": "The agent never guesses. It replies on the same thread, asking the A E for exactly what is missing, and stops.",
    "r01": "This is the reply the agent sent on the A E's thread. No call was placed, and no project was created.",
    "c12": "Negative run two. The A E first misses the call, and then gives an unclear answer about the plan tier.",
    "c13": "No answer. The agent waits, then calls again.",
    "c14": "This time the A E is unsure. An unclear answer is never accepted, so the agent calls once more.",
    "c15": "Still unclear. Retries are exhausted, so the run is escalated to the customer success manager by email and Slack. No project is created.",
    "c16": "Every step is in the audit log, with timestamp, input, output, decision and rationale.",
    "c17": "Forty five automated tests cover the happy path, validation, template accuracy, and the edge cases. Rocketlane down, an existing project, no answer, and an ambiguous tier.",
    "c18": "In production, a Twilio number rings the A E's mobile, Gmail and Retell push events through webhooks, Slack Connect invites the customer, and Salesforce supplies the A E phone and a tier cross check. Thank you.",
}

CAPTIONS = {  # on-screen text (shorter than narration)
    "c01": "NovaCRM onboarding · multi-agent system (Rocketlane FDE take-home)",
    "c02": "Intake Agent → voice tier check → Rocketlane · Communication Agent → Slack",
    "p01": "Positive run: the AE's deal email (Helios Freight) and the fields Claude extracts",
    "c03": "The deal email reaches the CS inbox",
    "c04": "Intake: Claude extraction + required-field guardrail",
    "c05": "Retell web call, not a phone line · AE voice is scripted · agent conversation is real",
    "c06": "Tier confirmed on read-back + independent transcript check → ACCEPT",
    "c07": "Rocketlane: Enterprise 30-day template, 15 tasks, dedicated CSM as Project Manager",
    "c08": "Communication Agent: personalized Slack channel (customer invite mocked)",
    "c09": "Rocketlane automation: overdue 1 day → Project Manager, 4 days → Project Owner",
    "a01": "Automation recipients, and the 4-day escalation that really fired",
    "g01": "Growth run, live: a real person answers as the AE (web call)",
    "g02": "AE confirms Growth → 14-day template, pooled CSM",
    "g03": "Rocketlane: Growth 14-day template, 15 tasks, pooled CSM",
    "g04": "Slack: Growth channel name, topic and welcome message",
    "c10": "Negative run 1: malformed contact email + non-Salesforce link",
    "c11": "Guardrail: ask the AE, never guess (reply on the thread)",
    "r01": "The agent's reply on the AE's thread · no call, no project",
    "c12": "Negative run 2: missed call, then an unclear tier",
    "c13": "No answer → retry",
    "c14": "Unclear answer → re-call",
    "c15": "Retries exhausted → escalate to CS manager (email + Slack) · no project",
    "c16": "Audit log: timestamp · input · output · decision · rationale",
    "c17": "45 automated tests",
    "c18": "Production changes",
}


def tts(keys: list[str] | None = None) -> None:
    NARR.mkdir(parents=True, exist_ok=True)
    for key, text in LINES.items():
        if keys and key not in keys:
            continue
        out = NARR / f"{key}.wav"
        ps = (
            "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.SelectVoice('Microsoft David Desktop'); $s.Rate = 1; "
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{out}', $f); $s.Speak(\"{text}\"); $s.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print(key, f"{dur(key):.1f}s")


def dur(key: str) -> float:
    with wave.open(str(NARR / f"{key}.wav")) as w:
        return w.getnframes() / w.getframerate()


def log(kind: str, value: str) -> None:
    with TIMELINE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "kind": kind, "value": value}) + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "tts":
        tts(sys.argv[2:] or None)
    elif cmd == "cap":
        key = sys.argv[2]
        log("cap", key)
        print(f"{dur(key):.1f}")
    elif cmd == "cut":
        log("cut_" + sys.argv[2], "")
    elif cmd == "dur":
        print(f"{dur(sys.argv[2]):.1f}")
    elif cmd == "mark":
        log("mark", sys.argv[2])
