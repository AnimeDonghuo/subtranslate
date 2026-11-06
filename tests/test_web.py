import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram import Bot
from telegram.error import TelegramError

from subtranslate.config import Settings
from subtranslate.web import WebhookReceiver, health, register_webhook

SECRET = "really_long_test_secret_123"


async def test_webhook_auth_deduplication_and_queue_backpressure():
    queue = asyncio.Queue(maxsize=1)
    receiver = WebhookReceiver(SECRET, Bot("12345:TESTTOKEN"), queue)
    app = web.Application(client_max_size=128 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_post("/telegram/webhook", receiver.receive)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await client.post("/telegram/webhook", json={"update_id": 1})).status == 403
        assert (await client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "é"},
                                  json={"update_id": 1})).status == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
        assert (await client.post("/telegram/webhook", headers=headers, data="{not-json")).status == 400
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": True})).status == 400
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": "bad"})).status == 400
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": 1})).status == 200
        # A retry of an accepted update must not consume another queue slot.
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": 1})).status == 200
        assert queue.qsize() == 1
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": 2})).status == 503
        assert queue.get_nowait().update_id == 1
        assert (await client.post("/telegram/webhook", headers=headers, json={"update_id": 2})).status == 200
        assert queue.get_nowait().update_id == 2


async def test_webhook_registration_retries_without_logging_secrets(monkeypatch, caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    token = "12345:DO_NOT_LOG_THIS_TOKEN"
    bot = SimpleNamespace(set_webhook=AsyncMock(side_effect=[TelegramError(token), True]))
    settings = Settings(token, "https://example.com")
    secret = settings.webhook_secret
    sleep = AsyncMock()
    monkeypatch.setattr("subtranslate.web.asyncio.sleep", sleep)
    await register_webhook(bot, settings)
    assert bot.set_webhook.await_count == 2
    assert bot.set_webhook.await_args.kwargs["url"] == "https://example.com/telegram/webhook"
    assert bot.set_webhook.await_args.kwargs["secret_token"] == secret
    sleep.assert_awaited_once_with(3)
    assert token not in caplog.text and secret not in caplog.text
