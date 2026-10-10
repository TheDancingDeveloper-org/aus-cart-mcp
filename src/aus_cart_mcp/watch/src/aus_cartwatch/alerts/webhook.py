"""JSON webhook notifier: POSTs `{kind, text, alerts: [...]}` (for myaiagent's inbound hook later, CW-52)."""

from __future__ import annotations

import httpx

from aus_cartwatch.alerts import Message


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0):
        self.url, self.transport, self.timeout = url, transport, timeout

    async def send(self, message: Message) -> None:
        body = {
            "kind": message.kind,
            "text": message.text,
            "alerts": [{"id": a["id"], "kind": a["kind"], **a["payload"]} for a in message.alerts],
        }
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as http:
            (await http.post(self.url, json=body)).raise_for_status()
