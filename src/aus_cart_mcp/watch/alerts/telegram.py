"""Telegram notifier and button handler, over the plain Bot API.

Messages go to every allowed chat id, with inline buttons (add 1 / add 2 /
snooze). aus_cartwatch is tailnet-only, so Telegram cannot reach a webhook:
button presses are read by long-polling `getUpdates`. Updates from any chat not
on the allow-list are ignored. The bot token is a secret: it is never logged
(it appears in the request path, so request logging stays off for this client).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from aus_cart_mcp.watch.alerts import Message
from aus_cart_mcp.watch.store import Store

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs, and the Bot API URL carries the token

API = "https://api.telegram.org"
OFFSET_SETTING = "telegram.offset"

ActionHandler = Callable[[str], Awaitable[str]]


class TelegramError(Exception):
    pass


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_ids: list[int],
        *,
        api: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ):
        if not token or not chat_ids:
            raise ValueError("a Telegram notifier needs a token and at least one chat id")
        self._token = token
        self.chat_ids = list(chat_ids)
        self.api = api.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"TelegramNotifier(chats={self.chat_ids})"

    async def call(self, method: str, payload: dict, *, timeout: float | None = None) -> dict | list:
        async with httpx.AsyncClient(transport=self.transport, timeout=timeout or self.timeout) as http:
            try:
                response = await http.post(f"{self.api}/bot{self._token}/{method}", json=payload)
            except httpx.HTTPError as exc:
                raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or response.status_code}")
        return data["result"]

    async def check(self) -> list[str]:
        """Readiness report lines: the bot's identity and whether each allowed chat accepts messages.

        Uses getMe and a silent 'typing' chat action, so nothing appears in the chat."""
        me = await self.call("getMe", {})
        lines = [f"bot @{me.get('username')} ({me.get('first_name')})"]
        for chat_id in self.chat_ids:
            try:
                await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                lines.append(f"chat {chat_id}: reachable")
            except TelegramError as exc:
                hint = (
                    " (open the bot in Telegram and press Start)"
                    if "initiate" in str(exc) or "not found" in str(exc)
                    else ""
                )
                lines.append(f"chat {chat_id}: NOT reachable: {exc}{hint}")
        return lines

    async def send(self, message: Message) -> None:
        markup = None
        if message.buttons:
            rows = [[{"text": label, "callback_data": action}] for label, action in message.buttons]
            if message.kind != "digest":  # one row: Add 1 · Add 2 · Snooze
                rows = [[button[0] for button in rows]]
            markup = {"inline_keyboard": rows}
        failures = []
        for chat_id in self.chat_ids:
            payload = {"chat_id": chat_id, "text": message.text[:4000], "disable_web_page_preview": True}
            if markup:
                payload["reply_markup"] = markup
            try:
                await self.call("sendMessage", payload)
            except TelegramError as exc:
                failures.append(str(exc))
        if len(failures) == len(self.chat_ids):
            raise TelegramError("; ".join(failures))

    async def poll_once(self, store: Store, handler: ActionHandler, *, timeout: int = 50) -> int:
        """Read pending updates once and handle allowed button presses. Returns how many were handled."""
        offset = int(store.get_setting(OFFSET_SETTING, "0") or 0)
        updates = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]},
            timeout=timeout + 10,
        )
        handled = 0
        for update in updates:
            store.set_setting(OFFSET_SETTING, str(update["update_id"] + 1))
            query = update.get("callback_query")
            if not query:
                continue
            chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
            if chat_id not in self.chat_ids:
                log.warning("ignoring a Telegram button press from a chat that is not allowed")
                continue
            try:
                outcome = await handler(str(query.get("data") or ""))
            except Exception as exc:
                log.exception("Telegram action failed")
                outcome = f"Failed: {type(exc).__name__}"
            handled += 1
            await self._acknowledge(query, outcome)
        return handled

    async def _acknowledge(self, query: dict, outcome: str) -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": outcome[:190]})
            message = query.get("message") or {}
            if message.get("message_id"):
                await self.call(
                    "editMessageText",
                    {
                        "chat_id": message["chat"]["id"],
                        "message_id": message["message_id"],
                        "text": f"{message.get('text', '')}\n\n→ {outcome}"[:4000],
                        "reply_markup": message.get("reply_markup") or {"inline_keyboard": []},
                        "disable_web_page_preview": True,
                    },
                )
        except TelegramError as exc:
            log.warning("could not acknowledge a Telegram button press: %s", exc)

    async def poll_forever(self, store: Store, handler: ActionHandler, *, pause: float = 5.0) -> None:
        log.info("Telegram button polling started")
        while True:
            try:
                await self.poll_once(store, handler)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram polling failed: %s", exc)
                await asyncio.sleep(pause)
