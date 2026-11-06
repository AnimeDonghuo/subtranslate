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
    monkeypatch.setattr("subtranslate.web._wait_for_retry", sleep)
    await register_webhook(bot, settings)
    assert bot.set_webhook.await_count == 2
    assert bot.set_webhook.await_args.kwargs["url"] == "https://example.com/telegram/webhook"
    assert bot.set_webhook.await_args.kwargs["secret_token"] == secret
    sleep.assert_awaited_once_with(3)
    assert token not in caplog.text and secret not in caplog.text


async def test_initialization_backoff_is_bounded_and_recovers(monkeypatch, caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, call

    from telegram.error import TimedOut

    from subtranslate.web import TelegramState, _connect_telegram

    token = "12345:DO_NOT_LOG_RETRY_TOKEN"
    bot = SimpleNamespace(shutdown=AsyncMock(), set_webhook=AsyncMock())
    telegram = SimpleNamespace(
        bot=bot,
        running=False,
        initialize=AsyncMock(side_effect=[TimedOut(token)] * 7 + [None]),
        start=AsyncMock(),
        shutdown=AsyncMock(),
    )
    service = SimpleNamespace(queue=SimpleNamespace(start=Mock()))
    state = TelegramState()
    retry = AsyncMock()
    monkeypatch.setattr("subtranslate.web._wait_for_retry", retry)
    await _connect_telegram(telegram, service, Settings(token, "https://example.com"), state)
    assert retry.await_args_list == [call(3), call(6), call(12), call(24), call(48), call(60), call(60)]
    assert telegram.initialize.await_count == 8
    assert bot.shutdown.await_count == 7
    service.queue.start.assert_called_once()
    assert state.accepting.is_set() and state.registered.is_set() and state.phase == "ready"
    assert token not in caplog.text


async def test_default_telegram_http_client_uses_longer_timeouts():
    from subtranslate.web import _build_telegram_application

    telegram = _build_telegram_application(Settings("12345:TESTTOKEN", "https://example.com"), asyncio.Queue())
    try:
        request = telegram.bot.request
        assert request.read_timeout == 30
        # PTB exposes the read timeout publicly; the rest are on its HTTPX client.
        assert request._client.timeout.connect == 30
        assert request._client.timeout.write == 30
        assert request._client.timeout.pool == 10
    finally:
        await asyncio.gather(*(transport.shutdown() for transport in telegram.bot._request))
