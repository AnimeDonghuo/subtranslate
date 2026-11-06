"""A small authenticated webhook listener suitable for a sleeping web service."""

import asyncio
import hmac
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from aiohttp import web
from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application
from telegram.request import BaseRequest

from .bot import BotService
from .config import Settings
from .languages import LanguageCatalog
from .website import WebsiteTranslator

logger = logging.getLogger(__name__)


@dataclass
class TelegramState:
    # Liveness stays available during retries, but Telegram updates are only
    # accepted after the application and its worker are running.
    accepting: asyncio.Event = field(default_factory=asyncio.Event)
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    phase: str = "connecting"


TELEGRAM_STATE = web.AppKey("telegram_state", TelegramState)


def _error_kind(exc: Exception) -> str:
    # Exception messages and request URLs can contain the bot token. The
    # underlying exception class is safe and helps distinguish connect/read
    # timeouts without exposing details of the request.
    name = type(exc).__name__
    if exc.__cause__ is not None:
        name += f", caused by {type(exc.__cause__).__name__}"
    return name


class WebhookReceiver:
    def __init__(self, secret: str, bot, update_queue: asyncio.Queue, *,
                 accepting: asyncio.Event | None = None, remembered_updates: int = 4096) -> None:
        self.secret = secret
        self.bot = bot
        self.update_queue = update_queue
        self.accepting = accepting
        self.remembered_updates = remembered_updates
        self.seen: OrderedDict[int, None] = OrderedDict()

    async def receive(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), self.secret.encode("ascii")):
            return web.Response(status=403, text="Forbidden")
        if self.accepting is not None and not self.accepting.is_set():
            # Do not acknowledge updates that no worker can process yet.
            # Telegram can retry them while the instance connects.
            return web.Response(status=503, text="Starting")
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
    """Liveness stays HTTP 200 while the bot connects or retries."""
    state = request.app.get(TELEGRAM_STATE)
    if state is None:
        return web.json_response({"status": "ok"})
    status = "ok" if state.registered.is_set() else ("error" if state.failed.is_set() else "starting")
    return web.json_response({"status": status, "telegram": state.phase})


async def readiness(request: web.Request) -> web.Response:
    """Readiness is separate from liveness so Koyeb can start the instance."""
    state = request.app[TELEGRAM_STATE]
    ready = state.registered.is_set()
    return web.json_response({"status": "ok" if ready else "not_ready", "telegram": state.phase},
                             status=200 if ready else 503)


async def register_webhook(bot, settings: Settings) -> bool:
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
            return True
        except InvalidToken:
            # PTB may include the full token in this exception; never log it.
            logger.error("Telegram rejected BOT_TOKEN while registering the webhook; check the token and redeploy")
            return False
        except (TelegramError, TimeoutError) as exc:
            logger.warning("Webhook registration failed (%s); retrying in %d seconds", _error_kind(exc), delay)
            await _wait_for_retry(delay)
            delay = min(delay * 2, 60)


async def _wait_for_retry(delay: float) -> None:
    await asyncio.sleep(delay)


async def _shutdown_telegram(telegram: Application) -> None:
    """Close Telegram resources after a failed start or during shutdown."""
    actions = [telegram.shutdown, telegram.bot.shutdown]
    if telegram.running:
        actions.insert(0, telegram.stop)
    # Application.shutdown is a no-op if Bot.initialize failed partway, so
    # always try Bot.shutdown as well, even if an earlier cleanup step fails.
    for action in actions:
        try:
            await action()
        except Exception as exc:  # noqa: BLE001 - cleanup errors can contain the bot token
            logger.warning("Telegram cleanup failed (%s)", _error_kind(exc))


async def _connect_telegram(telegram: Application, service: BotService, settings: Settings,
                            state: TelegramState) -> None:
    """Start the bot without blocking the HTTP listener or losing updates."""
    delay = 3
    logger.info("Connecting to Telegram in the background")
    while True:
        try:
            await telegram.initialize()
            await telegram.start()
            service.queue.start()
            state.accepting.set()
            state.phase = "registering_webhook"
            logger.info("Connected to Telegram; registering webhook")
            break
        except InvalidToken:
            # PTB's InvalidToken exception may contain the complete token.
            state.phase = "invalid_token"
            state.failed.set()
            logger.error("Telegram rejected BOT_TOKEN; check the BotFather token in Koyeb and redeploy")
            await _shutdown_telegram(telegram)
            return
        except Exception as exc:  # noqa: BLE001 - keep the web service alive during transient failures
            state.phase = "retrying"
            logger.warning("Telegram initialization failed (%s); retrying in %d seconds", _error_kind(exc), delay)
            await _shutdown_telegram(telegram)
            await _wait_for_retry(delay)
            delay = min(delay * 2, 60)
    try:
        registered = await register_webhook(telegram.bot, settings)
    except Exception as exc:  # noqa: BLE001 - never log exception details containing tokens
        state.accepting.clear()
        state.phase = "webhook_error"
        state.failed.set()
        logger.error("Telegram webhook registration failed (%s)", _error_kind(exc))
    else:
        if registered:
            state.phase = "ready"
            state.registered.set()
        else:
            state.accepting.clear()
            state.phase = "invalid_token"
            state.failed.set()


def _build_telegram_application(settings: Settings, update_queue: asyncio.Queue,
                                request: BaseRequest | None = None) -> Application:
    builder = Application.builder()
    if request is None:
        # PTB defaults to five seconds, which can be too short during a slow
        # free-instance cold start. File transfers have their own overrides.
        builder = builder.connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(10)
    else:
        # Inject a fake Telegram transport in integration tests.
        builder = builder.request(request).get_updates_request(request)
    return builder.token(settings.bot_token).update_queue(update_queue).concurrent_updates(8).build()


def create_web_app(settings: Settings, *, telegram_request: BaseRequest | None = None) -> web.Application:
    update_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    telegram = _build_telegram_application(settings, update_queue, telegram_request)
    catalog = LanguageCatalog()
    translator = WebsiteTranslator(
        settings.chromium_path,
        translation_timeout_ms=settings.translation_timeout_seconds * 1000,
    )
    service = BotService(settings, catalog, translator)
    service.install_handlers(telegram)
    state = TelegramState()
    receiver = WebhookReceiver(settings.webhook_secret, telegram.bot, update_queue, accepting=state.accepting)
    web_app = web.Application(client_max_size=128 * 1024)
    web_app[TELEGRAM_STATE] = state
    web_app.router.add_get("/", health)
    web_app.router.add_get("/healthz", health)
    web_app.router.add_get("/readyz", readiness)
    web_app.router.add_post("/telegram/webhook", receiver.receive)
    tasks: list[asyncio.Task] = []

    async def startup(app: web.Application) -> None:
        # Return immediately so the port can bind and Koyeb's liveness checks
        # pass even while Telegram is slow or unavailable.
        tasks.append(asyncio.create_task(_connect_telegram(telegram, service, settings, state), name="connect-telegram"))
        tasks.append(asyncio.create_task(catalog.refresh(), name="refresh-languages"))

    async def cleanup(app: web.Application) -> None:
        state.accepting.clear()
        state.registered.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Do not delete the webhook: Telegram must still be able to wake the
        # service when Koyeb scales the instance to zero.
        if telegram.running:
            try:
                await telegram.stop()
            except Exception as exc:  # noqa: BLE001 - do not log request or token details
                logger.warning("Telegram stop failed (%s)", _error_kind(exc))
        try:
            await service.shutdown()
        except Exception as exc:  # noqa: BLE001 - still attempt to close the bot
            logger.warning("Subtitle service cleanup failed (%s)", _error_kind(exc))
        # Also close a bot that failed or was canceled during initialization.
        await _shutdown_telegram(telegram)

    web_app.on_startup.append(startup)
    web_app.on_cleanup.append(cleanup)
    return web_app
