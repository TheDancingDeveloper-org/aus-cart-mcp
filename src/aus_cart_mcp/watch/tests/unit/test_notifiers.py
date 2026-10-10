import json
import logging

import httpx
import pytest

from aus_cart_mcp.watch.alerts import Message
from aus_cart_mcp.watch.alerts.telegram import TelegramError, TelegramNotifier
from aus_cart_mcp.watch.alerts.webhook import WebhookNotifier

TOKEN = "123456:SECRET-TOKEN"
ALERT = {"id": 7, "kind": "heavy_discount", "payload": {"name": "Coffee", "price": 5.0}}


class FakeBot:
    """The Telegram Bot API as an httpx transport."""

    def __init__(self, updates=None, fail_chats=()):
        self.requests: list[tuple[str, dict]] = []
        self.updates = list(updates or [])
        self.fail_chats = set(fail_chats)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        body = json.loads(request.content or b"{}")
        self.requests.append((method, body))
        if method == "sendMessage" and body["chat_id"] in self.fail_chats:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        if method == "getUpdates":
            pending, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": pending})
        return httpx.Response(200, json={"ok": True, "result": True})

    def notifier(self, chats=(1,)):
        return TelegramNotifier(TOKEN, list(chats), transport=httpx.MockTransport(self))


def press(update_id, chat_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"q{update_id}",
            "data": data,
            "message": {"message_id": 5, "chat": {"id": chat_id}, "text": "🔥 Heavy discount: Coffee"},
        },
    }


async def test_send_with_buttons_to_every_chat():
    bot = FakeBot()
    message = Message("heavy_discount", "🔥 Coffee $5", [ALERT], [("Add 1 to cart", "add:7:1"), ("Snooze", "snooze:7")])
    await bot.notifier(chats=(1, 2)).send(message)
    assert [r[1]["chat_id"] for r in bot.requests] == [1, 2]
    assert bot.requests[0][1]["reply_markup"]["inline_keyboard"] == [
        [{"text": "Add 1 to cart", "callback_data": "add:7:1"}, {"text": "Snooze", "callback_data": "snooze:7"}]
    ]


async def test_digest_buttons_are_one_per_row_and_partial_failure_is_ok():
    bot = FakeBot(fail_chats={2})
    message = Message("digest", "2 on sale", [ALERT], [("+1 Coffee", "add:7:1"), ("+1 Tea", "add:8:1")])
    await bot.notifier(chats=(1, 2)).send(message)
    assert len(bot.requests[0][1]["reply_markup"]["inline_keyboard"]) == 2


async def test_send_fails_when_every_chat_fails():
    bot = FakeBot(fail_chats={1})
    with pytest.raises(TelegramError, match="chat not found"):
        await bot.notifier().send(Message("operator", "x", [ALERT]))


async def test_button_presses_from_allowed_chats_only(store, caplog):
    bot = FakeBot([press(10, 1, "add:7:1"), press(11, 666, "add:7:5"), {"update_id": 12, "message": {}}])
    seen = []

    async def handler(action):
        seen.append(action)
        return "Added 1 × Coffee"

    with caplog.at_level(logging.DEBUG):
        handled = await bot.notifier().poll_once(store, handler, timeout=0)
    assert handled == 1 and seen == ["add:7:1"]
    assert store.get_setting("telegram.offset") == "13"
    methods = [m for m, _ in bot.requests]
    assert methods == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert bot.requests[2][1]["text"].endswith("→ Added 1 × Coffee")
    assert TOKEN not in caplog.text


async def test_handler_failure_is_reported_to_the_owner(store):
    bot = FakeBot([press(1, 1, "add:7:1")])

    async def handler(action):
        raise RuntimeError("boom")

    await bot.notifier().poll_once(store, handler, timeout=0)
    assert bot.requests[1][1]["text"] == "Failed: RuntimeError"


async def test_transport_errors_hide_the_token():
    def broken(request):
        raise httpx.ConnectError("no route", request=request)

    notifier = TelegramNotifier(TOKEN, [1], transport=httpx.MockTransport(broken))
    with pytest.raises(TelegramError) as info:
        await notifier.call("getMe", {})
    assert TOKEN not in str(info.value) and TOKEN not in repr(notifier)
    bad = TelegramNotifier(TOKEN, [1], transport=httpx.MockTransport(lambda r: httpx.Response(502, text="x")))
    with pytest.raises(TelegramError, match="HTTP 502"):
        await bad.call("getMe", {})


def test_notifier_needs_token_and_chats():
    with pytest.raises(ValueError):
        TelegramNotifier("", [1])
    with pytest.raises(ValueError):
        TelegramNotifier(TOKEN, [])


async def test_webhook_posts_json():
    received = []

    def hook(request):
        received.append(json.loads(request.content))
        return httpx.Response(204)

    await WebhookNotifier("http://hook.test/x", transport=httpx.MockTransport(hook)).send(
        Message("heavy_discount", "text", [ALERT])
    )
    assert received == [
        {
            "kind": "heavy_discount",
            "text": "text",
            "alerts": [{"id": 7, "kind": "heavy_discount", "name": "Coffee", "price": 5.0}],
        }
    ]


async def test_check_reports_bot_and_chat_reachability():
    def api(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "acw_bot", "first_name": "ACW"}})
        if body.get("chat_id") == 2:
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot can't initiate conversation"})
        return httpx.Response(200, json={"ok": True, "result": True})

    lines = await TelegramNotifier(TOKEN, [1, 2], transport=httpx.MockTransport(api)).check()
    assert lines[0] == "bot @acw_bot (ACW)" and lines[1] == "chat 1: reachable"
    assert "NOT reachable" in lines[2] and "press Start" in lines[2]


def test_cli_without_token(monkeypatch, capsys):
    from aus_cart_mcp.watch.__main__ import main

    monkeypatch.delenv("AUS_CARTWATCH_TELEGRAM_TOKEN", raising=False)
    assert main(["telegram", "check"]) == 2
    monkeypatch.setenv("AUS_CARTWATCH_TELEGRAM_TOKEN", "t")
    monkeypatch.delenv("AUS_CARTWATCH_TELEGRAM_CHAT_IDS", raising=False)
    assert main(["telegram", "test"]) == 2
