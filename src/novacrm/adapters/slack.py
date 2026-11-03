"""Slack Web API client (bot token). Works on the free plan; Slack Connect invites need a paid plan."""
from __future__ import annotations

from typing import Optional

import httpx


class SlackError(Exception):
    pass


class SlackClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url="https://slack.com/api/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=20,
            transport=transport,
        )

    def _call(self, method: str, *, get: bool = False, ok_errors: tuple[str, ...] = (), **payload) -> dict:
        resp = self.http.get(method, params=payload) if get else self.http.post(method, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok") and body.get("error") not in ok_errors:
            raise SlackError(f"{method}: {body.get('error')}")
        return body

    def find_channel(self, name: str) -> Optional[str]:
        cursor = None
        while True:
            params = {"types": "public_channel", "exclude_archived": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = self._call("conversations.list", get=True, **params)
            for ch in body.get("channels", []):
                if ch["name"] == name:
                    return ch["id"]
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None

    def create_channel(self, name: str) -> str:
        return self._call("conversations.create", name=name, is_private=False)["channel"]["id"]

    def set_topic(self, channel_id: str, topic: str) -> None:
        self._call("conversations.setTopic", channel=channel_id, topic=topic[:250])

    def set_purpose(self, channel_id: str, purpose: str) -> None:
        self._call("conversations.setPurpose", channel=channel_id, purpose=purpose[:250])

    def post_message(self, channel: str, text: str) -> None:
        self._call("chat.postMessage", channel=channel, text=text, unfurl_links=False)

    def lookup_user(self, email: str) -> Optional[str]:
        body = self._call("users.lookupByEmail", get=True, ok_errors=("users_not_found",), email=email)
        return body["user"]["id"] if body.get("ok") else None

    def invite(self, channel_id: str, user_ids: list[str]) -> None:
        if user_ids:
            self._call("conversations.invite", ok_errors=("already_in_channel",), channel=channel_id, users=",".join(user_ids))

    def join(self, channel_id: str) -> None:
        self._call("conversations.join", ok_errors=("already_in_channel", "method_not_supported_for_channel_type"),
                   channel=channel_id)
