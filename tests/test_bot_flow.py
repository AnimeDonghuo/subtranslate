"""Exercise real PTB handlers with a fake Telegram transport, without credentials."""

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram.ext import Application
from telegram.request import BaseRequest

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.languages import LanguageCatalog
from subtranslate.subtitles import verify_and_merge
from subtranslate.web import WebhookReceiver

SOURCE = b"10\n00:00:01,000 --> 00:00:02,000\nHello world\n\n"
TRANSLATED = "1\n00:00:01,000 --> 00:00:02,000\nनमस्ते दुनिया\n\n".encode()
USER = {"id": 42, "is_bot": False, "first_name": "User"}
CHAT = {"id": 42, "type": "private"}
BOT_USER = {"id": 12345, "is_bot": True, "first_name": "Bot", "username": "example_bot"}
DOCUMENT_MESSAGE = {
    "message_id": 77, "date": 1700000000, "chat": CHAT, "from": USER,
    "document": {"file_id": "file1", "file_unique_id": "unique1", "file_name": "movie.srt", "file_size": len(SOURCE)},
}


class TelegramStub(BaseRequest):
    def __init__(self):
        self.calls = []
        self.picker_sent = asyncio.Event()
        self.document_sent = asyncio.Event()
        self.webhook_set = asyncio.Event()
        self.next_message = 100
        self.uploads = []

    @property
    def read_timeout(self):
        return 10.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if method == "GET":
            return 200, SOURCE
        action = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((action, params))
        if action == "getMe":
            result = BOT_USER
        elif action == "getFile":
            result = {"file_id": "file1", "file_unique_id": "unique1", "file_size": len(SOURCE), "file_path": "test.srt"}
        elif action in {"answerCallbackQuery", "setWebhook"}:
            if action == "setWebhook":
                self.webhook_set.set()
            result = True
        elif action in {"sendMessage", "editMessageText", "sendDocument"}:
            self.next_message += 1
            result = {"message_id": self.next_message, "date": 1700000000, "chat": CHAT, "from": BOT_USER}
            if action == "sendDocument":
                self.uploads.append(request_data.multipart_data)
                result["document"] = {"file_id": "out", "file_unique_id": "outunique", "file_name": "result.srt"}
                self.document_sent.set()
            else:
                result["text"] = params.get("text", "")
                if params.get("reply_markup"):
                    self.picker_sent.set()
        else:
            raise AssertionError(f"Unexpected Telegram method {action}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class FakeTranslator:
    async def translate(self, source, code):
        assert code == "hi"
        return verify_and_merge(source, TRANSLATED)


def callback_update(update_id=2, code="hi"):
    # Telegram attaches the document to the bot's reply. No bot-side session
    # or local file is needed, even if the process restarted in between.
    picker = {
        "message_id": 100, "date": 1700000001, "chat": CHAT, "from": BOT_USER,
        "text": "Choose the language", "reply_to_message": DOCUMENT_MESSAGE,
    }
    return {"update_id": update_id, "callback_query": {
        "id": f"callback-{update_id}", "from": USER, "message": picker,
        "chat_instance": "42", "data": f"lang:{code}",
    }}


async def test_document_to_button_to_download_and_return():
    request = TelegramStub()
    queue = asyncio.Queue(maxsize=128)
    telegram = (Application.builder().token("12345:TESTTOKEN").request(request)
                .get_updates_request(request).update_queue(queue).concurrent_updates(8).build())
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com", allowed_user_ids=frozenset({42}))
    service = BotService(settings, LanguageCatalog(), FakeTranslator())
    service.install_handlers(telegram)
    receiver = WebhookReceiver(settings.webhook_secret, telegram.bot, queue)
    app = web.Application()
    app.router.add_post("/telegram/webhook", receiver.receive)
    await telegram.initialize()
    await telegram.start()
    service.queue.start()
    try:
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
            response = await client.post("/telegram/webhook", headers=headers, json={"update_id": 1, "message": DOCUMENT_MESSAGE})
            assert response.status == 200
            await asyncio.wait_for(request.picker_sent.wait(), 2)
            picker_calls = [params for action, params in request.calls if action == "sendMessage" and params.get("reply_markup")]
            assert picker_calls[0]["reply_parameters"]["message_id"] == DOCUMENT_MESSAGE["message_id"]
            response = await client.post("/telegram/webhook", headers=headers, json=callback_update())
            assert response.status == 200
            await asyncio.wait_for(request.document_sent.wait(), 2)
            assert any(action == "getFile" and params["file_id"] == "file1" for action, params in request.calls)
            send = next(params for action, params in request.calls if action == "sendDocument")
            assert send["chat_id"] == 42
            assert "Hindi" in send["caption"]
            upload = next(iter(request.uploads[0].values()))
            assert upload[0] == "movie.Hindi.srt"
            assert "नमस्ते दुनिया".encode() in upload[1]
            assert b"00:00:01,000 --> 00:00:02,000" in upload[1]
    finally:
        await telegram.stop()
        await service.queue.close()
        await telegram.shutdown()


async def test_language_callback_works_after_process_restart():
    request = TelegramStub()
    queue = asyncio.Queue(maxsize=128)
    telegram = (Application.builder().token("12345:TESTTOKEN").request(request)
                .get_updates_request(request).update_queue(queue).build())
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com")
    service = BotService(settings, LanguageCatalog(), FakeTranslator())
    service.install_handlers(telegram)
    await telegram.initialize()
    await telegram.start()
    service.queue.start()
    try:
        # Start with an empty process and only the callback from Telegram.
        await telegram.update_queue.put(__import__("telegram").Update.de_json(callback_update(), telegram.bot))
        await asyncio.wait_for(request.document_sent.wait(), 2)
        assert request.uploads
    finally:
        await telegram.stop()
        await service.queue.close()
        await telegram.shutdown()


async def test_web_service_registers_webhook_and_handles_a_callback(monkeypatch):
    from unittest.mock import AsyncMock, patch

    import subtranslate.web as bot_web

    request = TelegramStub()
    builder = Application.builder().request(request).get_updates_request(request)
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com")
    # Exercise the real web-service lifecycle with a fake Telegram transport.
    with patch.object(Application, "builder", return_value=builder), \
         patch.object(bot_web, "WebsiteTranslator", return_value=FakeTranslator()), \
         patch.object(LanguageCatalog, "refresh", new=AsyncMock()):
        app = bot_web.create_web_app(settings)
        async with TestClient(TestServer(app)) as client:
            await asyncio.wait_for(request.webhook_set.wait(), 2)
            registration = next(params for action, params in request.calls if action == "setWebhook")
            assert registration["url"] == "https://example.com/telegram/webhook"
            assert registration["secret_token"] == settings.webhook_secret
            assert registration["drop_pending_updates"] is False
            assert set(registration["allowed_updates"]) == {"message", "callback_query"}
            assert (await client.get("/healthz")).status == 200
            headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
            assert (await client.post("/telegram/webhook", headers=headers, json=callback_update(11))).status == 200
            await asyncio.wait_for(request.document_sent.wait(), 2)
    assert not any(action == "deleteWebhook" for action, _ in request.calls)


async def test_full_webhook_bot_and_real_browser_with_mock_site(mock_site, chromium_path):
    from unittest.mock import AsyncMock, patch

    import subtranslate.web as bot_web
    from subtranslate.website import WebsiteTranslator

    request = TelegramStub()
    builder = Application.builder().request(request).get_updates_request(request)
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com")
    browser = WebsiteTranslator(chromium_path, site_url=mock_site, stage_timeout_ms=5000,
                                translation_timeout_ms=1500, change_wait_ms=800)
    with patch.object(Application, "builder", return_value=builder), \
         patch.object(bot_web, "WebsiteTranslator", return_value=browser), \
         patch.object(LanguageCatalog, "refresh", new=AsyncMock()):
        async with TestClient(TestServer(bot_web.create_web_app(settings))) as client:
            await asyncio.wait_for(request.webhook_set.wait(), 2)
            headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
            await client.post("/telegram/webhook", headers=headers, json={"update_id": 20, "message": DOCUMENT_MESSAGE})
            await asyncio.wait_for(request.picker_sent.wait(), 2)
            await client.post("/telegram/webhook", headers=headers, json=callback_update(21))
            await asyncio.wait_for(request.document_sent.wait(), 15)
            upload = next(iter(request.uploads[0].values()))
            assert upload[0] == "movie.Hindi.srt"
            assert b"Hola mundo" in upload[1]  # The local mock's fake translation.
            assert b"00:00:01,000 --> 00:00:02,000" in upload[1]


async def test_graceful_web_shutdown_notifies_active_and_waiting_jobs():
    from unittest.mock import AsyncMock, patch

    import subtranslate.web as bot_web

    started = asyncio.Event()

    class SlowTranslator:
        async def translate(self, source, code):
            started.set()
            await asyncio.Event().wait()

    request = TelegramStub()
    builder = Application.builder().request(request).get_updates_request(request)
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com")
    with patch.object(Application, "builder", return_value=builder), \
         patch.object(bot_web, "WebsiteTranslator", return_value=SlowTranslator()), \
         patch.object(LanguageCatalog, "refresh", new=AsyncMock()):
        async with TestClient(TestServer(bot_web.create_web_app(settings))) as client:
            await asyncio.wait_for(request.webhook_set.wait(), 2)
            headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
            assert (await client.post("/telegram/webhook", headers=headers, json=callback_update(30))).status == 200
            await asyncio.wait_for(started.wait(), 2)
            assert (await client.post("/telegram/webhook", headers=headers, json=callback_update(31, "es"))).status == 200

            async def wait_for_queued_spanish():
                while not any(action == "sendMessage" and params.get("text", "").startswith("Queued for Spanish")
                              for action, params in request.calls):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_queued_spanish(), 2)
        # The web cleanup is graceful here; a hard kill cannot send notices.
        notices = [params["text"] for action, params in request.calls
                   if action == "sendMessage" and "was interrupted by a restart" in params.get("text", "")]
        assert len(notices) == 2
        assert any("Hindi" in text for text in notices)
        assert any("Spanish" in text for text in notices)
        assert not request.uploads


async def test_invalid_bot_token_is_not_exposed_in_startup_errors(caplog):
    import traceback
    from unittest.mock import patch

    import subtranslate.web as bot_web

    class RejectingTelegramStub(TelegramStub):
        async def do_request(self, url, method, request_data=None, **kwargs):
            if url.endswith("/getMe"):
                return 401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}'
            return await super().do_request(url, method, request_data, **kwargs)

    token = "12345:VERY_PRIVATE_TOKEN"
    request = RejectingTelegramStub()
    builder = Application.builder().request(request).get_updates_request(request)
    settings = Settings(token, "long_secret_token_123", "https://example.com")
    with patch.object(Application, "builder", return_value=builder):
        server = TestServer(bot_web.create_web_app(settings))
        try:
            try:
                await server.start_server()
            except RuntimeError as exc:
                assert "Telegram initialization failed" in str(exc)
                assert token not in "".join(traceback.format_exception(exc))
                assert token not in caplog.text
            else:
                raise AssertionError("An invalid bot token should prevent startup")
        finally:
            await server.close()
