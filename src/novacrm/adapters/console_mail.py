"""Mail client that prints instead of sending; used for offline demos (`--mail console`)."""
from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.panel import Panel

from ..models import InboundEmail

_console = Console()


class ConsoleMail:
    def __init__(self, inbox: list[InboundEmail] | None = None):
        self.inbox = list(inbox or [])
        self.sent: list[dict] = []
        self.labels: dict[str, set[str]] = {}

    def list_new(self, query: str) -> list[InboundEmail]:
        new = [e for e in self.inbox if "novacrm-seen" not in self.labels.get(e.message_id, set())]
        return new

    def thread_text(self, thread_id: str) -> str:
        parts = [e for e in self.inbox if e.thread_id == thread_id]
        return "\n\n---\n\n".join(f"From: {e.sender_name} <{e.sender_email}>\nSubject: {e.subject}\n\n{e.body}" for e in parts)

    def add_labels(self, message_id: str, labels: list[str]) -> None:
        self.labels.setdefault(message_id, set()).update(labels)

    def reply(self, email: InboundEmail, body: str, reply_to: Optional[str] = None) -> None:
        self.sent.append({"to": email.sender_email, "subject": f"Re: {email.subject}", "body": body, "thread": email.thread_id})
        _console.print(Panel(body, title=f"[green]reply -> {email.sender_email}", expand=False))

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        _console.print(Panel(body, title=f"[red]email -> {to}: {subject}", expand=False))
