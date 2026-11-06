"""A small authenticated webhook listener suitable for a sleeping web service."""

import asyncio
import hmac
import logging
from collections import OrderedDict

from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from .bot import BotService
from .config import Settings
from .languages import LanguageCatalog
from .website import WebsiteTranslator

logger = logging.getLogger(__name__)


class WebhookReceiver:
    def __init__(self, secret: str, bot, update_queue: asyncio.Queue, *, remembered_updates: int = 4096) -> None:
        self.secret = secret
        self.bot = bot
        self.update_queue = update_queue
        self.remembered_updates = remembered_updates
        self.seen: OrderedDict[int, None] = OrderedDict()

    async def receive(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), self.secret.encode("ascii")):
            return web.Response(status=403, text="Forbidden")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or type(payload.get("update_id")) is not int:
                raise ValueError("Invalid update")
            update_id = payload["update_id"]
            if update_id in self.seen:
                return web.Response(text="OK")
            update = Update.de_json(payload, self.bot)
            if update is None:
                raise ValueError("Invalid update")
        except (ValueError, TypeError, KeyError, AttributeError):
            return web.Response(status=400, text="Invalid update")
        try:
            self.update_queue.put_nowait(update)
        except asyncio.QueueFull:
            # Returning 503 makes Telegram retry instead of dropping the update.
            return web.Response(status=503, text="Busy")
        self.seen[update_id] = None
        if len(self.seen) > self.remembered_updates:
            self.seen.popitem(last=False)
        return web.Response(text="OK")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def register_webhook(bot, settings: Settings) -> None:
    delay = 3
    while True:
        try:
            await bot.set_webhook(
                url=settings.webhook_url,
                secret_token=settings.webhook_secret,
                allowed_updates=["message", "callback_query"],
                max_connections=4,
                drop_pending_updates=False,
            )
            logger.info("Telegram webhook registered")
            return
        except TelegramError as exc:
            logger.warning("Webhook registration failed (%s); retrying", type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def create_web_app(settings: Settings) -> web.Application:
    update_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    telegram = (
        Application.builder()
        .token(settings.bot_token)
        .update_queue(update_queue)
        .concurrent_updates(8)
        .build()
    )
    catalog = LanguageCatalog()
    translator = WebsiteTranslator(
        settings.chromium_path,
        translation_timeout_ms=settings.translation_timeout_seconds * 1000,
    )
    service = BotService(settings, catalog, translator)
    service.install_handlers(telegram)
    receiver = WebhookReceiver(settings.webhook_secret, telegram.bot, update_queue)
    web_app = web.Application(client_max_size=128 * 1024)
    web_app.router.add_get("/", health)
    web_app.router.add_get("/healthz", health)
    web_app.router.add_post("/telegram/webhook", receiver.receive)
    tasks: list[asyncio.Task] = []

    async def startup(app: web.Application) -> None:
        try:
            await telegram.initialize()
        except Exception as exc:  # noqa: BLE001 - PTB exceptions can include the bot token
            logger.error("Telegram initialization failed (%s); check credentials and connectivity", type(exc).__name__)
            try:
                # Application.initialize can fail after the bot's HTTP clients
                # have been opened but before the application is initialized.
                await telegram.bot.shutdown()
            except Exception as shutdown_exc:  # noqa: BLE001 - do not log sensitive exception details
                logger.warning("Telegram cleanup failed (%s)", type(shutdown_exc).__name__)
            # PTB's InvalidToken message includes the complete token. Suppress
            # the original exception chain so startup logs never print it.
            raise RuntimeError("Telegram initialization failed; check BOT_TOKEN and network access") from None
        await telegram.start()
        service.queue.start()
        tasks.append(asyncio.create_task(register_webhook(telegram.bot, settings), name="register-webhook"))
        tasks.append(asyncio.create_task(catalog.refresh(), name="refresh-languages"))

    async def cleanup(app: web.Application) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Do not delete the webhook: Telegram must still be able to wake the
        # service when Koyeb scales the instance to zero.
        if telegram.running:
            await telegram.stop()
        await service.shutdown()
        await telegram.shutdown()

    web_app.on_startup.append(startup)
    web_app.on_cleanup.append(cleanup)
    return web_app
