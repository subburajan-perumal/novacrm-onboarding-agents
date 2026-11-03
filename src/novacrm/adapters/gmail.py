"""Gmail API client: poll the CS inbox, read threads, label, reply in-thread, send escalations."""
from __future__ import annotations

import base64
import html
import re
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ..models import InboundEmail

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _b64decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _body(payload: dict) -> str:
    plain, rich = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64decode(data))
        elif data and mime == "text/html":
            rich.append(_strip_html(_b64decode(data)))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain) if plain else "\n".join(rich)


def _drop_quoted(text: str) -> str:
    """Remove quoted history so a reply's text isn't double counted."""
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:$", line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        creds = None
        if Path(token_file).exists():
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            Path(token_file).parent.mkdir(parents=True, exist_ok=True)
            Path(token_file).write_text(creds.to_json())
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._label_ids: dict[str, str] = {}

    # ---- labels ------------------------------------------------------------
    def _label_id(self, name: str) -> str:
        if not self._label_ids:
            for lab in self.svc.users().labels().list(userId="me").execute().get("labels", []):
                self._label_ids[lab["name"]] = lab["id"]
        if name not in self._label_ids:
            lab = self.svc.users().labels().create(userId="me", body={"name": name}).execute()
            self._label_ids[name] = lab["id"]
        return self._label_ids[name]

    def add_labels(self, message_id: str, labels: list[str]) -> None:
        ids = [self._label_id(n) for n in labels]
        self.svc.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": ids}).execute()

    # ---- read --------------------------------------------------------------
    def _to_email(self, msg: dict) -> InboundEmail:
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        name, addr = parseaddr(headers.get("from", ""))
        return InboundEmail(
            message_id=msg["id"],
            thread_id=msg["threadId"],
            sender_email=addr.lower(),
            sender_name=name,
            to=headers.get("to", ""),
            subject=headers.get("subject", ""),
            body=_body(msg["payload"]),
            received_at=headers.get("date", ""),
            rfc822_id=headers.get("message-id", ""),
        )

    def list_new(self, query: str) -> list[InboundEmail]:
        resp = self.svc.users().messages().list(userId="me", q=query, maxResults=20).execute()
        out = []
        for ref in reversed(resp.get("messages", [])):  # oldest first
            msg = self.svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            out.append(self._to_email(msg))
        return out

    def thread_text(self, thread_id: str) -> str:
        thread = self.svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        parts = []
        for msg in thread.get("messages", []):
            e = self._to_email(msg)
            parts.append(f"From: {e.sender_name} <{e.sender_email}>\nSubject: {e.subject}\n\n{_drop_quoted(e.body)}")
        return "\n\n---\n\n".join(parts)

    # ---- write -------------------------------------------------------------
    def _send_raw(self, mime: MIMEText, thread_id: Optional[str] = None) -> None:
        body = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        self.svc.users().messages().send(userId="me", body=body).execute()

    def reply(self, email: InboundEmail, body: str, reply_to: Optional[str] = None) -> None:
        mime = MIMEText(body)
        mime["To"] = email.sender_email
        mime["Subject"] = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
        if email.rfc822_id:
            mime["In-Reply-To"] = email.rfc822_id
            mime["References"] = email.rfc822_id
        if reply_to:
            mime["Reply-To"] = reply_to
        self._send_raw(mime, email.thread_id)

    def send(self, to: str, subject: str, body: str) -> None:
        mime = MIMEText(body)
        mime["To"] = to
        mime["Subject"] = subject
        self._send_raw(mime)
