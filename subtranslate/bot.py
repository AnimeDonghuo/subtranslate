"""Telegram interaction and job processing."""

import asyncio
import html
import logging
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import aiohttp
from telegram import InputFile, Message, ReplyParameters, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .admin import install_admin_handlers
from .ass_export import MAX_ASS_SOURCE_BYTES, ass_filename, srt_to_ass
from .config import FREE_PRESET_LIMIT, Settings
from .fileio import run_file_io, run_scan, wait_for_cleanup
from .jobs import EnqueueResult, JobQueue, TranslationJob
from .languages import LanguageCatalog, confirm_all_keyboard, language_keyboard
from .large_srt import prepare_file_srt
from .large_telegram import (
    BOT_API_DOWNLOAD_BYTES,
    BOT_API_UPLOAD_BYTES,
    LargeTelegramTransfer,
    LargeTransferError,
)
from .menus import install_menu_handlers
from .progress import ProgressMessage
from .proxies import parse_proxy_candidates
from .status_board import StatusBoards
from .store import Database
from .subtitles import (
    MAX_INPUT_BYTES,
    SubtitleError,
    prepare_subtitle,
    subtitle_extension,
    translated_filename,
)
from .tasks import TaskRegistry
from .website import LanguageUnavailable, WebsiteError, WebsiteTranslator

logger = logging.getLogger(__name__)

PICK = "Choose the language you want for this subtitle:"
PREMIUM_ALERT = (
    "🔒 Select all languages is for the bot owner and premium users. "
    "Ask the owner to enable premium with /addpremium."
)
MAX_PROXY_INPUT_BYTES = 19 * 1024 * 1024
MAX_PROXY_ENTRIES = 80_000


class JobCancelled(Exception):
    """A user canceled the active translation or transfer."""


class BotService:
    def __init__(self, settings: Settings, catalog: LanguageCatalog, translator: WebsiteTranslator,
                 store: Database | None = None) -> None:
        self.settings = settings
        self.catalog = catalog
        self.translator = translator
        # Without MongoDB the store keeps everything in memory, so the bot
        # still starts and translates when the database is not configured.
        self.store = store or Database(default_mode=settings.default_access_mode)
        self._preset_drafts: dict[int, set[str]] = {}
        # Live task numbers for /status and /cancel_id (never persisted).
        self.tasks = TaskRegistry()
        # One self-refreshing /status message per chat.
        self.status_boards = StatusBoards()
        self.queue = JobQueue(
            self.process_job, max_waiting=2,
            max_concurrent=settings.max_concurrent_jobs, min_free_mb=settings.min_free_mb,
        )
        # A chat can have two jobs running at once, so each map holds a set.
        self._batch_cancellations: dict[int, set[asyncio.Event]] = {}
        self._single_cancellations: dict[int, set[asyncio.Event]] = {}
        self._proxy_input_chats: set[int] = set()
        # Chats that ran /ass and are expected to send an SRT next.
        self._ass_waiting: set[int] = set()
        self._proxy_scan_task: asyncio.Task | None = None
        self._proxy_scan_status: dict[str, int] = {}
        self.bot = None

    def install_handlers(self, application) -> None:
        self.bot = application.bot
        application.add_handler(CommandHandler("id", self.user_id))
        application.add_handler(CommandHandler("cancel", self.cancel_batch))
        application.add_handler(CommandHandler("proxychk", self.proxy_command))
        # Menus and owner commands use specific callback patterns. They are
        # registered before the catch-all job callback so it cannot swallow them.
        install_menu_handlers(self, application)
        install_admin_handlers(self, application)
        application.add_handler(MessageHandler(filters.Document.ALL, self.document))
        application.add_handler(CallbackQueryHandler(self.callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.other_text))
        application.add_error_handler(self.on_error)

    async def shutdown(self) -> None:
        """Best-effort notices for jobs interrupted by a graceful shutdown."""
        await self.status_boards.close_all()
        await self._cancel_proxy_scan()
        interrupted = await self.queue.close()
        if not interrupted or self.bot is None:
            return
        try:
            # Koyeb's shutdown grace period is finite. Don't hold the process
            # open indefinitely if Telegram is unavailable during shutdown.
            async with asyncio.timeout(5):
                results = await asyncio.gather(*(
                    self._notice(
                        job.chat_id,
                        f"Translation to {job.language_name} was interrupted by a restart. "
                        "Please tap the language button again or resend the file.",
                    )
                    for job in interrupted
                ), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        logger.warning("Could not send a shutdown notice (%s)", type(result).__name__)
        except TimeoutError:
            logger.warning("Timed out notifying users of interrupted translations")

    def _is_owner(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.settings.owner_ids

    async def is_premium(self, user_id: int | None) -> bool:
        """Owners always count as premium; premium lifts the free limits."""
        if not user_id:
            return False
        if user_id in self.settings.owner_ids:
            return True
        return await self.store.is_premium(user_id)

    def _permitted(self, user_id: int | None) -> bool:
        """Owners and authorized users always pass; a ban always applies."""
        if not user_id:
            return False
        if user_id in self.settings.owner_ids:
            return True
        if self.store.is_banned(user_id):
            return False
        if self.settings.allowed_user_ids and user_id in self.settings.allowed_user_ids:
            return True
        if self.store.is_authorized(user_id):
            return True
        return self.store.mode == "public"

    async def _remember(self, user) -> None:
        """Keep the sender's current name for /banlist and the leaderboard."""
        if user is None:
            return
        await self.store.touch_user(user.id, getattr(user, "full_name", "") or "",
                                    getattr(user, "username", "") or "")

    def _track_cancel(self, mapping: dict[int, set[asyncio.Event]], chat_id: int,
                      cancel: asyncio.Event) -> None:
        mapping.setdefault(chat_id, set()).add(cancel)

    def _untrack_cancel(self, mapping: dict[int, set[asyncio.Event]], chat_id: int,
                        cancel: asyncio.Event) -> None:
        events = mapping.get(chat_id)
        if events is None:
            return
        events.discard(cancel)
        if not events:
            mapping.pop(chat_id, None)

    def cancel_user_work(self, chat_id: int) -> tuple[int, bool]:
        """Drop this chat's queued jobs and stop every job it is running."""
        removed = self.queue.cancel_waiting_jobs(chat_id)
        events = set(self._batch_cancellations.get(chat_id, ()))             | set(self._single_cancellations.get(chat_id, ()))
        for cancel in events:
            cancel.set()
        return removed, bool(events)

    def preset_draft(self, user_id: int) -> set[str]:
        return self._preset_drafts.setdefault(user_id, set())

    def drop_preset_draft(self, user_id: int) -> None:
        self._preset_drafts.pop(user_id, None)

    async def _preset_codes(self, user_id: int | None) -> tuple[str, ...]:
        return await self.store.presets(user_id)

    async def _is_private_and_allowed(self, update: Update) -> bool:
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not message or not chat or not user:
            return False
        if chat.type != "private":
            await message.reply_text("Please send subtitle files to me in a private chat.")
            return False
        await self._remember(user)
        if self.store.is_banned(user.id) and not self._is_owner(user.id):
            await message.reply_text(
                "🚫 You are banned from using this bot. Contact the owner if you believe "
                "this is a mistake."
            )
            return False
        if not self._permitted(user.id):
            await message.reply_text(
                "🔒 This bot is currently private. Ask the owner to authorize your Telegram "
                "user ID (send /id to see it)."
            )
            return False
        return True

    async def ensure_allowed(self, update: Update) -> bool:
        """Public entry point used by the menu and owner modules."""
        return await self._is_private_and_allowed(update)

    async def ensure_callback_allowed(self, query) -> bool:
        """The button equivalent of ensure_allowed, without leaking details."""
        user = query.from_user
        message = query.message
        if user is None:
            await query.answer("Please use the bot in a private chat.", show_alert=True)
            return False
        if isinstance(message, Message) and (message.chat.type != "private"
                                             or message.chat.id != user.id):
            await query.answer("Please use the bot in a private chat.", show_alert=True)
            return False
        await self._remember(user)
        if self.store.is_banned(user.id) and not self._is_owner(user.id):
            await query.answer("🚫 You are banned from using this bot.", show_alert=True)
            return False
        if not self._permitted(user.id):
            await query.answer("🔒 This bot is currently private.", show_alert=True)
            return False
        return True

    async def user_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Allow an owner to discover their ID before configuring the allowlist.
        if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
            await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")

    async def cancel_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        chat_id = update.effective_chat.id
        removed, active = self.cancel_user_work(chat_id)
        if active or removed:
            await update.effective_message.reply_text(
                "Canceled queued jobs. Active work is stopping; no unfinished subtitle will be sent.\n"
                "Use /status to see task numbers and /cancel_id to stop one task."
            )
        else:
            await update.effective_message.reply_text("You have no active or queued translation job.")

    async def _proxy_admin_allowed(self, update: Update) -> bool:
        if not await self._is_private_and_allowed(update):
            return False
        # Proxy configuration affects every user's traffic. Never let an
        # unrestricted public bot give arbitrary users that control.
        if self.settings.allowed_user_ids is None and not self._is_owner(update.effective_user.id):
            await update.effective_message.reply_text(
                "Set ALLOWED_USER_IDS on the deployment before using /proxychk. "
                "Every listed user can change the bot-wide proxy pool."
            )
            return False
        return True

    async def _cancel_proxy_scan(self) -> bool:
        task = self._proxy_scan_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def proxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._proxy_admin_allowed(update):
            return
        chat_id = update.effective_chat.id
        args = getattr(context, "args", None) or []
        command = args[0].lower() if len(args) == 1 else ""
        if command == "direct":
            self._proxy_input_chats.discard(chat_id)
            await self._cancel_proxy_scan()
            await self._apply_proxy_input(update, "direct")
        elif command == "cancel":
            self._proxy_input_chats.discard(chat_id)
            canceled = await self._cancel_proxy_scan()
            await update.effective_message.reply_text(
                "Proxy scan canceled. If it had already finished, its checked pool may already be active." if canceled
                else "Proxy input canceled; the existing configuration is unchanged."
            )
        elif command == "status":
            task = self._proxy_scan_task
            if task is None or task.done():
                await update.effective_message.reply_text("No proxy scan is running.")
            elif not self._proxy_scan_status:
                await update.effective_message.reply_text("Preparing the proxy list.")
            else:
                status = self._proxy_scan_status
                await update.effective_message.reply_text(
                    f"Proxy scan: {status['checked']:,}/{status['total']:,} checked, "
                    f"{status['working']:,} passed; {status['invalid']:,} invalid entries skipped."
                )
        elif args:
            await update.effective_message.reply_text("Use /proxychk, /proxychk status, /proxychk direct, or /proxychk cancel.")
        else:
            self._proxy_input_chats.add(chat_id)
            await update.effective_message.reply_text(
                "Send up to 80,000 HTTP, HTTPS, SOCKS4, SOCKS5 or VLESS links as text or a UTF-8 file "
                "up to 19 MiB, one per line or comma-separated. Only test proxies you are authorized to use. "
                "Use /proxychk status, /proxychk cancel, or /proxychk direct. "
                "This changes the bot-wide pool; Telegram bot chats are not end-to-end encrypted."
            )

    async def _run_proxy_scan(self, message, value: str) -> None:
        heartbeat = asyncio.create_task(self._keep_active_job_awake(), name="proxy-scan-heartbeat")
        try:
            proxies, invalid = await asyncio.to_thread(parse_proxy_candidates, value, MAX_PROXY_ENTRIES)
            del value
            if not proxies:
                await message.reply_text("No valid or supported proxy entries were found; the existing pool is unchanged.")
                return
            self._proxy_scan_status = {"checked": 0, "total": len(proxies), "working": 0, "invalid": invalid}

            def progress(checked: int, working: int) -> None:
                self._proxy_scan_status.update(checked=checked, working=working)

            count = await self.translator.configure_proxies(proxies, on_progress=progress)
            if count:
                await message.reply_text(
                    f"Proxy scan finished: {count:,} of {len(proxies):,} passed; {invalid:,} invalid entries skipped. "
                    "The passing proxies are now in the bot-wide pool. This does not guarantee translation will work."
                )
            else:
                await message.reply_text(
                    f"No proxies passed ({len(proxies):,} checked, {invalid:,} invalid skipped); "
                    "the existing configuration is unchanged."
                )
        except asyncio.CancelledError:
            raise
        except ValueError:
            await message.reply_text("Proxy list exceeds 80,000 entries; the existing pool is unchanged.")
        except TelegramError:
            logger.warning("Could not send proxy scan status to Telegram")
        except Exception as exc:  # noqa: BLE001 - never log endpoints or credentials
            logger.warning("Proxy scan failed (%s)", type(exc).__name__)
            try:
                await message.reply_text("Could not check the proxies. The existing configuration is unchanged.")
            except TelegramError:
                pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _apply_proxy_input(self, update: Update, value: str) -> None:
        message = update.effective_message
        if len(value.encode("utf-8")) > MAX_PROXY_INPUT_BYTES:
            await message.reply_text("Proxy input is too large (maximum 19 MiB). Use /proxychk to try again.")
            return
        if value.strip().lower() == "direct":
            await self._cancel_proxy_scan()
            try:
                await self.translator.configure_proxies(())
            except WebsiteError:
                await message.reply_text("Could not change proxy settings; the existing pool is unchanged.")
                return
            await message.reply_text("Direct server connection enabled for future translations.")
            return
        if self._proxy_scan_task is not None and not self._proxy_scan_task.done():
            await message.reply_text("A proxy scan is already running. Use /proxychk status or /proxychk cancel.")
            return
        self._proxy_scan_status = {}
        self._proxy_scan_task = asyncio.create_task(self._run_proxy_scan(message, value), name="proxy-scan")
        await message.reply_text(
            "Proxy scan started in the background. It may take hours or days for large lists. "
            "Use /proxychk status or /proxychk cancel. The existing pool remains active until the scan finishes."
        )

    async def other_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        chat_id = update.effective_chat.id
        if chat_id in self._proxy_input_chats:
            self._proxy_input_chats.discard(chat_id)
            if await self._proxy_admin_allowed(update):
                await self._apply_proxy_input(update, update.effective_message.text or "")
        else:
            await update.effective_message.reply_text("Send a subtitle file as a document, then choose a language. /help")

    def wait_for_ass(self, chat_id: int) -> None:
        self._ass_waiting.add(chat_id)

    async def convert_to_ass(self, message: Message, *, plate: bool = False) -> None:
        """Convert the SRT attached to this message and send back a styled ASS."""
        doc = message.document
        if doc is None or self.bot is None:
            return
        if doc.file_size is not None and doc.file_size > MAX_ASS_SOURCE_BYTES:
            await message.reply_text("That SRT is too large to convert; keep it under 25 MB.")
            return
        job = TranslationJob(
            chat_id=message.chat.id,
            original_message_id=message.message_id,
            file_id=doc.file_id,
            file_name=doc.file_name or "subtitle.srt",
            file_size=doc.file_size,
            language_code="ass",
            language_name="ASS",
        )
        try:
            async with self._job_resources() as (directory, transport):
                if doc.file_size is not None and doc.file_size > BOT_API_DOWNLOAD_BYTES:
                    path = directory / "source.srt"
                    await transport.download(job, path)
                    data = await run_file_io(path.read_bytes)
                else:
                    telegram_file = await self.bot.get_file(doc.file_id)
                    data = bytes(await telegram_file.download_as_bytearray(read_timeout=120))
            content, cue_count = await run_file_io(srt_to_ass, data, plate)
        except (SubtitleError, LargeTransferError) as exc:
            await message.reply_text(f"Could not convert: {exc}")
            return
        except TelegramError as exc:
            logger.warning("ASS conversion download failed (%s)", type(exc).__name__)
            await message.reply_text("Telegram could not download the SRT. Please resend it and try again.")
            return
        except OSError as exc:
            logger.warning("Temporary storage failed during ASS conversion (%s)", type(exc).__name__)
            await message.reply_text("Could not use temporary storage. Check disk space and try again.")
            return
        filename = ass_filename(job.file_name)
        escaped = html.escape(filename, quote=False)
        await message.reply_document(
            document=InputFile(BytesIO(content), filename=filename),
            caption=(f"🎬 <b>ASS ready</b> · {cue_count:,} cues\n"
                     f"Hardsub: <code>ffmpeg -i input.mp4 -vf \"ass={escaped}\" -c:a copy output.mp4</code>"),
            parse_mode="HTML",
        )

    async def document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        message = update.effective_message
        doc = message.document
        chat_id = update.effective_chat.id
        if chat_id in self._proxy_input_chats:
            self._proxy_input_chats.discard(chat_id)
            if not await self._proxy_admin_allowed(update):
                return
            if doc is None or doc.file_size is None or not 0 < doc.file_size <= MAX_PROXY_INPUT_BYTES:
                await message.reply_text("Send a non-empty UTF-8 proxy file of at most 19 MiB. Use /proxychk to retry.")
                return
            try:
                telegram_file = await self.bot.get_file(doc.file_id)
                data = await telegram_file.download_as_bytearray(read_timeout=60)
                if len(data) != doc.file_size or len(data) > MAX_PROXY_INPUT_BYTES:
                    raise ValueError
                value = bytes(data).decode("utf-8-sig")
            except (TelegramError, UnicodeError, ValueError):
                await message.reply_text("Could not read the proxy file. Use /proxychk to retry; the pool is unchanged.")
                return
            await self._apply_proxy_input(update, value)
            return
        try:
            extension = subtitle_extension(doc.file_name if doc and doc.file_name else "")
        except SubtitleError as exc:
            await message.reply_text(str(exc))
            return
        if doc.file_size is not None and doc.file_size <= 0:
            await message.reply_text("Please send a non-empty subtitle file.")
            return
        if extension != ".srt" and doc.file_size is not None and doc.file_size > MAX_INPUT_BYTES:
            await message.reply_text("Non-SRT formats are limited to 1 MB. Try SRT for larger files.")
            return
        if chat_id in self._ass_waiting:
            self._ass_waiting.discard(chat_id)
            if extension != ".srt":
                await message.reply_text("Send an SRT file for /ass; other formats cannot be converted.")
                return
            await self.convert_to_ass(message)
            return
        # Explicitly reply to the original document. Telegram includes it in
        # callback messages, so a language selection survives Koyeb sleeping.
        markup, page, pages = language_keyboard(
            self.catalog, presets=await self._preset_codes(update.effective_user.id),
            allow_all=await self.is_premium(update.effective_user.id),
        )
        await message.reply_text(
            f"{PICK}\n\nPage {page}/{pages}",
            reply_markup=markup,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False),
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not await self.ensure_callback_allowed(query):
            return
        message = query.message
        user = query.from_user
        data = query.data or ""
        if data.startswith("page:"):
            try:
                page_number = int(data.removeprefix("page:"))
                markup, current, total = language_keyboard(
                    self.catalog, page_number, presets=await self._preset_codes(user.id),
                    allow_all=await self.is_premium(user.id),
                )
            except ValueError:
                await query.answer("Please choose a language again.", show_alert=True)
                return
            await query.answer()
            await self._edit_picker(query, f"{PICK}\n\nPage {current}/{total}", markup)
            return
        if (data == "all:locked" or data.startswith("all:confirm:")) and not await self.is_premium(user.id):
            await query.answer(PREMIUM_ALERT, show_alert=True)
            return
        if data.startswith("all:confirm:"):
            try:
                page_number = int(data.removeprefix("all:confirm:"))
            except ValueError:
                await query.answer("Please choose a language again.", show_alert=True)
                return
            await query.answer()
            await self._edit_picker(
                query,
                f"Translate to all {len(self.catalog.items())} languages, one at a time? "
                "This can take hours and send many files. The free instance may restart or sleep. "
                "Use /cancel to stop a batch.",
                confirm_all_keyboard(page_number),
            )
            return
        languages = ()
        if data == "all:start":
            if not await self.is_premium(user.id):
                await query.answer(PREMIUM_ALERT, show_alert=True)
                return
            languages = tuple(self.catalog.items())
            code, name = "*", "all languages"
        elif data == "preset:start":
            saved = await self._preset_codes(user.id)
            languages = tuple((value, self.catalog.get(value)) for value in saved
                              if self.catalog.get(value))
            if not languages:
                await query.answer(
                    "You have no saved preset languages yet. Open /ussetting to add some.",
                    show_alert=True,
                )
                return
            if len(languages) > FREE_PRESET_LIMIT and not await self.is_premium(user.id):
                # Premium may have expired after a larger list was saved.
                languages = languages[:FREE_PRESET_LIMIT]
                await query.answer(
                    f"Free accounts translate up to {FREE_PRESET_LIMIT} saved languages; "
                    "only the first batch was queued.",
                    show_alert=True,
                )
            code, name = "*", "preset languages"
        elif data.startswith("lang:"):
            code = data.removeprefix("lang:")
            name = self.catalog.get(code)
            if not name:
                await query.answer("This language is no longer in the list. Please resend the file.", show_alert=True)
                return
        else:
            await query.answer("Please choose a language again.", show_alert=True)
            return
        original = message.reply_to_message
        if not original or not original.document or not original.from_user or original.from_user.id != user.id:
            await query.answer("The original file is no longer available. Please send it again.", show_alert=True)
            return
        doc = original.document
        try:
            extension = subtitle_extension(doc.file_name or "")
        except SubtitleError:
            await query.answer("Please send a supported subtitle file again.", show_alert=True)
            return
        if doc.file_size is not None and doc.file_size <= 0:
            await query.answer("The file must not be empty.", show_alert=True)
            return
        if extension != ".srt" and doc.file_size is not None and doc.file_size > MAX_INPUT_BYTES:
            await query.answer("Non-SRT formats are limited to 1 MB. Try SRT for larger files.", show_alert=True)
            return
        job = TranslationJob(
            chat_id=message.chat.id,
            original_message_id=original.message_id,
            file_id=doc.file_id,
            file_name=doc.file_name,
            file_size=doc.file_size,
            language_code=code,
            language_name=name,
            languages=languages,
            user_id=user.id,
            user_name=getattr(user, "full_name", "") or "",
            username=getattr(user, "username", "") or "",
        )
        # Number the job for /status and /cancel_id before it reaches the queue.
        task = self.tasks.register(
            job_key=job.key, chat_id=job.chat_id, user_id=job.user_id,
            user_name=job.user_name, username=job.username,
            file_name=job.file_name or "", language=name,
            is_batch=bool(languages), batch_total=len(languages),
            now=asyncio.get_running_loop().time(),
        )
        job = replace(job, task_id=task.id)
        status = self.queue.submit(job)
        if status is not EnqueueResult.ACCEPTED:
            self.tasks.remove(task.id)
        if status is EnqueueResult.DUPLICATE:
            await query.answer("That translation is already queued or running.")
        elif status is EnqueueResult.FULL:
            await query.answer("The queue is full. Please tap again after a job finishes.", show_alert=True)
        else:
            await query.answer(f"Queued for {name}")
            if languages:
                label = (f"your {len(languages)} saved languages" if data == "preset:start"
                         else f"{len(languages)} languages")
                await message.reply_text(
                    f"Queued {label} as task #{task.id}. A stage bar will appear when the batch starts. "
                    "I’ll send each file one by one. This may take hours; use /cancel or "
                    f"/cancel_id {task.id} to stop."
                )
            else:
                await message.reply_text(
                    f"Queued for {name} as task #{task.id}. A stage bar will appear when the job starts. "
                    f"This may take several minutes on the free instance; /cancel_id {task.id} stops it."
                )
            if self.bot is not None:
                # Show this user's live status straight away; it replaces their
                # previous status message and refreshes itself.
                await self.status_boards.show(self, chat_id=message.chat.id, viewer_id=user.id)

    async def _edit_picker(self, query, text, markup) -> None:
        try:
            await query.edit_message_text(text=text, reply_markup=markup)
        except BadRequest as exc:
            # Double taps can produce an identical edit. Do not log Telegram
            # URLs, request bodies or users' file names.
            logger.info("Language picker could not be updated (%s)", type(exc).__name__)

    async def _notice(self, chat_id: int, text: str) -> bool:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
            return True
        except TelegramError as exc:
            logger.warning("Could not send a Telegram status (%s)", type(exc).__name__)
            return False

    async def _finish_progress(self, progress: ProgressMessage, chat_id: int, text: str) -> None:
        if not await progress.finish(text):
            # If Telegram refuses an edit, try a normal message instead.
            await self._notice(chat_id, text)

    @asynccontextmanager
    async def _job_resources(self):
        # Subtitle text and MTProto authorization must not outlive the job.
        with tempfile.TemporaryDirectory(prefix="subtranslate-job-") as directory:
            transport = LargeTelegramTransfer(self.settings)
            try:
                yield Path(directory), transport
            finally:
                await transport.close()

    async def _download_source(self, job: TranslationJob, directory: Path, transport: LargeTelegramTransfer):
        extension = subtitle_extension(job.file_name)
        path = directory / "source.srt"
        if extension == ".srt" and job.file_size is not None and job.file_size > BOT_API_DOWNLOAD_BYTES:
            await transport.download(job, path)
            return await run_scan(prepare_file_srt, path)
        try:
            telegram_file = await self.bot.get_file(job.file_id)
        except BadRequest:
            # Some updates do not include size; the public endpoint can reject
            # an oversized file before returning metadata. Try the original
            # message over MTProto if credentials are configured.
            if extension != ".srt" or not self.settings.api_id or not self.settings.api_hash:
                raise
            await transport.download(job, path)
            return await run_scan(prepare_file_srt, path)
        file_size = telegram_file.file_size if telegram_file.file_size is not None else job.file_size
        if file_size is not None and file_size <= 0:
            raise SubtitleError("The subtitle file is empty.")
        if extension != ".srt" and file_size is not None and file_size > MAX_INPUT_BYTES:
            raise SubtitleError("Non-SRT formats are limited to 1 MB. Try SRT for larger files.")
        if extension == ".srt" and file_size is not None and file_size > BOT_API_DOWNLOAD_BYTES:
            await transport.download(job, path)
            return await run_scan(prepare_file_srt, path)
        data = await telegram_file.download_as_bytearray(read_timeout=60)
        if not data or (file_size is not None and len(data) != file_size):
            raise SubtitleError("The Telegram download was empty or incomplete. Please send the file again.")
        if job.file_size is not None and len(data) != job.file_size:
            raise SubtitleError("The Telegram download did not match the original size. Please send it again.")
        if extension == ".srt":
            await run_file_io(path.write_bytes, data)
            del data
            return await run_scan(prepare_file_srt, path)
        return await asyncio.to_thread(prepare_subtitle, bytes(data), job.file_name)

    async def _send_result(self, job: TranslationJob, name: str, result, transport: LargeTelegramTransfer,
                           progress: str = "") -> None:
        if result.possibly_incomplete:
            raise SubtitleError("A long section of the website download remained unchanged; no potentially incomplete file was sent.")
        filename = translated_filename(job.file_name, name)
        cue_word = "cue" if result.cue_count == 1 else "cues"
        caption = f"{progress}{name} · {result.cue_count:,} {cue_word} checked"
        if result.changed_cues is not None:
            caption += (f"\n{result.changed_cues:,}/{result.cue_count:,} cue texts differ from the original. "
                        "Names or shared words may stay the same.")
        caption += "\nFrom the website's Download button. Please review the machine translation."
        if result.unchanged:
            caption += (
                "\n\n⚠️ The website returned unchanged text. It may already be in the target "
                "language, or translation may have failed."
            )
        if isinstance(result.content, Path):
            if result.content.stat().st_size > BOT_API_UPLOAD_BYTES:
                await transport.send(job, result.content, filename, caption)
            else:
                # PTB otherwise reads an open handle into RAM at construction.
                with result.content.open("rb") as source:
                    await self.bot.send_document(
                        chat_id=job.chat_id,
                        document=InputFile(source, filename=filename, read_file_handle=False),
                        caption=caption,
                        reply_parameters=ReplyParameters(message_id=job.original_message_id,
                                                         allow_sending_without_reply=True),
                        write_timeout=120, read_timeout=120,
                    )
        elif len(result.content) > BOT_API_UPLOAD_BYTES:
            # SRT downloads normally use a file. Never accidentally pass an
            # oversized in-memory result to the public Bot API.
            raise LargeTransferError("The translated file is too large for the ordinary Telegram upload. Please retry.")
        else:
            await self.bot.send_document(
                chat_id=job.chat_id,
                document=InputFile(BytesIO(result.content), filename=filename),
                caption=caption,
                reply_parameters=ReplyParameters(message_id=job.original_message_id, allow_sending_without_reply=True),
                write_timeout=120, read_timeout=120,
            )
        await self._after_delivery(job, name, filename, result, transport)

    async def _after_delivery(self, job: TranslationJob, name: str, filename: str, result,
                              transport: LargeTelegramTransfer | None = None) -> None:
        """Record usage and keep a copy for the owner's log channel."""
        size = (result.content.stat().st_size if isinstance(result.content, Path)
                else len(result.content))
        if job.user_id:
            await self.store.record_translation(
                user_id=job.user_id, name=job.user_name, username=job.username,
                language_code=job.language_code, language_name=name,
                cue_count=result.cue_count, filename=filename, size=size,
            )
        await self._log_channel_copy(job, name, filename, result, size, transport)

    async def _log_channel_copy(self, job: TranslationJob, name: str, filename: str,
                                result, size: int,
                                transport: LargeTelegramTransfer | None = None) -> None:
        # Best effort: a missing or full log channel never affects delivery.
        channel = self.settings.log_channel_id
        if channel is None:
            return
        username = f"@{html.escape(job.username)}" if job.username else "not set"
        caption = (
            "📥 <b>Translated subtitle delivered</b>\n\n"
            f"👤 Name: {html.escape(job.user_name or 'Unknown')}\n"
            f"🆔 User ID: <code>{job.user_id or 'unknown'}</code>\n"
            f"🔗 Username: {username}\n"
            f"🌐 Language: {html.escape(name)}\n"
            f"📄 File: {html.escape(job.file_name or 'subtitle')} → {html.escape(filename)}\n"
            f"🎬 Cues: {result.cue_count:,}"
        )
        try:
            if isinstance(result.content, Path) and size > BOT_API_UPLOAD_BYTES:
                # The Bot API cannot upload this, so reuse the 2 GB MTProto path.
                if transport is None:
                    raise LargeTransferError("The large-file transport is unavailable.")
                await transport.send_file_to(channel, result.content, filename, caption)
            elif isinstance(result.content, Path):
                with result.content.open("rb") as source:
                    await self.bot.send_document(
                        chat_id=channel,
                        document=InputFile(source, filename=filename, read_file_handle=False),
                        caption=caption, parse_mode="HTML", read_timeout=120, write_timeout=120,
                    )
            elif size > BOT_API_UPLOAD_BYTES:
                raise LargeTransferError("The file is too large for the log channel copy.")
            else:
                await self.bot.send_document(
                    chat_id=channel,
                    document=InputFile(BytesIO(result.content), filename=filename),
                    caption=caption, parse_mode="HTML", read_timeout=120, write_timeout=120,
                )
        except (TelegramError, OSError, LargeTransferError) as exc:
            logger.warning("Log channel copy failed (%s)", type(exc).__name__)
            try:
                await self.bot.send_message(
                    chat_id=channel,
                    text=caption + "\n\n⚠️ The file copy could not be sent to this channel.",
                    parse_mode="HTML",
                )
            except TelegramError as notice_error:
                logger.info("Log channel notice failed (%s)", type(notice_error).__name__)

    @staticmethod
    async def _cancelable(awaitable, cancel: asyncio.Event):
        if cancel.is_set():
            awaitable.close()
            raise JobCancelled()
        operation = asyncio.create_task(awaitable)
        stopped = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait((operation, stopped), return_when=asyncio.FIRST_COMPLETED)
            # If delivery finished at the same instant as cancellation, do not
            # claim it failed: Telegram may already have delivered the file.
            if operation in done:
                return await operation
            raise JobCancelled()
        finally:
            for task in (operation, stopped):
                if not task.done():
                    task.cancel()
            cleanup = asyncio.gather(operation, stopped, return_exceptions=True)
            # Shutdown can race with /cancel. Do not remove job files until
            # canceled transfers/scans have actually finished cleaning up.
            await wait_for_cleanup(cleanup)

    async def _translate_with_deadline(self, translator, source, code: str, progress: ProgressMessage):
        # No deadline for the entire job. Bound an inactive website stage and
        # renew the watchdog when the one-upload flow advances to another stage.
        seconds = self.settings.translation_timeout_seconds
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(seconds) as deadline:
            def update(stage: str) -> None:
                # Parsing and translation have their own inactivity timeouts;
                # potentially large file operations are cancellable and not
                # given a fixed total deadline.
                if stage in {"starting the browser", "checking website proxies", "uploading and parsing the subtitle", "translating",
                             "waiting for translation to settle", "downloading", "validating the downloaded file"}:
                    deadline.reschedule(None)
                else:
                    deadline.reschedule(loop.time() + seconds)
                progress.set_stage(stage)

            return await translator.translate(source, code, on_progress=update)

    async def process_job(self, job: TranslationJob) -> None:
        try:
            await self._process_one(job)
        finally:
            # Single jobs and batches both pass through here.
            self.tasks.remove(job.task_id)

    async def _process_one(self, job: TranslationJob) -> None:
        if job.languages:
            await self._process_all(job)
            return
        cancel = asyncio.Event()
        self._track_cancel(self._single_cancellations, job.chat_id, cancel)
        # /cancel_id 12 sets this exact event.
        self.tasks.mark_running(job.task_id, cancel, now=asyncio.get_running_loop().time())
        try:
            progress = await ProgressMessage.start(self.bot, job.chat_id, job.language_name)
        except BaseException:
            self._untrack_cancel(self._single_cancellations, job.chat_id, cancel)
            raise
        if progress is None:
            self._untrack_cancel(self._single_cancellations, job.chat_id, cancel)
            return
        self.tasks.attach_progress(job.task_id, progress)
        heartbeat = asyncio.create_task(self._keep_active_job_awake(), name="job-heartbeat")
        try:
            try:
                async with self._job_resources() as (directory, transport):
                    source = await self._cancelable(self._download_source(job, directory, transport), cancel)
                    result = await self._cancelable(
                        self._translate_with_deadline(self.translator, source, job.language_code, progress), cancel
                    )
                    progress.set_stage("sending result")
                    await self._cancelable(self._send_result(job, job.language_name, result, transport), cancel)
                cue_word = "cue" if result.cue_count == 1 else "cues"
                if result.unchanged or result.possibly_incomplete:
                    final = (f"⚠️ Sent {job.language_name} with {result.cue_count:,} checked {cue_word}, "
                             "but some text may be untranslated. Please review the file.")
                else:
                    final = f"✅ {job.language_name} complete. Sent the file with {result.cue_count:,} checked {cue_word}."
            except JobCancelled:
                final = "Translation canceled. No unfinished subtitle was sent."
            except (SubtitleError, WebsiteError, LargeTransferError) as exc:
                final = f"Could not translate: {exc}\n\nYou can tap a language button to try again."
            except TimeoutError:
                final = "The website timed out. No incomplete file was sent; please retry."
            except TelegramError as exc:
                logger.warning("Telegram file transfer failed (%s)", type(exc).__name__)
                final = "Telegram could not download or send the file. Please resend it and try again."
            except OSError as exc:
                logger.warning("Temporary subtitle storage failed (%s)", type(exc).__name__)
                final = "Could not use temporary storage. Check available disk space; no incomplete file was sent."
            except Exception as exc:  # noqa: BLE001 - do not log data/URLs from third parties
                logger.error("Translation failed (%s)", type(exc).__name__)
                final = "Something went wrong while translating. Please try again later."
            await self._finish_progress(progress, job.chat_id, final)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await progress.close()
            self._untrack_cancel(self._single_cancellations, job.chat_id, cancel)

    async def _keep_active_job_awake(self) -> None:
        # A job is real work, but Koyeb only sees incoming HTTP traffic. Ping
        # the public health route while it runs; stop immediately when it ends.
        # This is best effort and does not make the in-memory job durable.
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(20 * 60)
                try:
                    async with session.get(self.settings.webhook_base_url + "/healthz") as response:
                        await response.read()
                except (aiohttp.ClientError, TimeoutError) as exc:
                    logger.info("Job heartbeat unavailable (%s)", type(exc).__name__)

    async def _process_all(self, job: TranslationJob) -> None:
        try:
            async with self._job_resources() as (directory, transport):
                await self._process_all_with_resources(job, directory, transport)
        except OSError as exc:
            logger.warning("Batch temporary storage failed (%s)", type(exc).__name__)
            await self._notice(job.chat_id, "Could not use temporary storage. Check available disk space and retry.")

    async def _process_all_with_resources(self, job: TranslationJob, directory: Path,
                                          transport: LargeTelegramTransfer) -> None:
        cancel = asyncio.Event()
        self._track_cancel(self._batch_cancellations, job.chat_id, cancel)
        self.tasks.mark_running(job.task_id, cancel, now=asyncio.get_running_loop().time())
        heartbeat = None
        progress = None
        delivered = skipped = failed = consecutive_failures = 0
        skipped_names: list[str] = []
        failed_names: list[str] = []
        failure_details: list[tuple[str, str]] = []
        stopped = session_failed = repeated_failures = False
        try:
            progress = await ProgressMessage.start(
                self.bot, job.chat_id, job.language_name or "all languages",
                batch_total=len(job.languages),
            )
            if progress is None:
                return
            self.tasks.attach_progress(job.task_id, progress)
            heartbeat = asyncio.create_task(self._keep_active_job_awake(), name="batch-heartbeat")
            try:
                source = await self._cancelable(self._download_source(job, directory, transport), cancel)
            except JobCancelled:
                await self._finish_progress(progress, job.chat_id, "Translation batch canceled. No unfinished file was sent.")
                return
            except (SubtitleError, LargeTransferError) as exc:
                await self._finish_progress(progress, job.chat_id, f"Could not read the subtitle: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - notify the user if the batch cannot start
                logger.warning("Could not start all-language batch (%s)", type(exc).__name__)
                await self._finish_progress(progress, job.chat_id, "Could not read the subtitle file. Please resend it and try again.")
                return
            if cancel.is_set():
                stopped = True
            else:
                try:
                    progress.set_stage("starting the browser")
                    # Batch state stays here while each language uses and
                    # disposes of its own browser and website context.
                    async with self.translator.session() as translator:
                        for number, (code, name) in enumerate(job.languages, start=1):
                            if cancel.is_set():
                                stopped = True
                                break
                            progress.set_language(name, number, delivered + skipped + failed)
                            progress.set_stage("starting the browser")
                            result = None
                            try:
                                result = await self._cancelable(
                                    self._translate_with_deadline(translator, source, code, progress), cancel
                                )
                                progress.set_stage("sending result")
                                await self._cancelable(
                                    self._send_result(job, name, result, transport, progress=f"{number}/{len(job.languages)} · "),
                                    cancel,
                                )
                                delivered += 1
                                consecutive_failures = 0
                            except JobCancelled:
                                stopped = True
                                break
                            except LanguageUnavailable:
                                skipped += 1
                                skipped_names.append(name)
                                # A stale catalog entry is not a website outage.
                                consecutive_failures = 0
                            except (TelegramError, LargeTransferError) as exc:
                                failed += 1
                                failed_names.append(name)
                                if len(failure_details) < 8 and isinstance(exc, LargeTransferError):
                                    failure_details.append((name, str(exc)[:300]))
                                stopped = True
                                logger.warning("Telegram batch delivery failed (%s)", type(exc).__name__)
                                break
                            except Exception as exc:  # noqa: BLE001 - prevent repeated failures from running all day
                                failed += 1
                                failed_names.append(name)
                                if len(failure_details) < 8 and isinstance(exc, (SubtitleError, WebsiteError)):
                                    failure_details.append((name, str(exc)[:300]))
                                consecutive_failures += 1
                                logger.warning("Batch language failed (%s)", type(exc).__name__)
                                if consecutive_failures >= 3:
                                    stopped = repeated_failures = True
                                    break
                            finally:
                                if result is not None and isinstance(result.content, Path):
                                    result.content.unlink(missing_ok=True)
                                progress.set_completed(delivered + skipped + failed)
                except Exception as exc:  # noqa: BLE001 - session startup/cleanup can also fail
                    stopped = session_failed = True
                    logger.warning("Batch browser session failed (%s)", type(exc).__name__)
            if cancel.is_set():
                stopped = True
            suffix = "Stopped" if stopped else "Finished"
            summary = f"{suffix}: {delivered} file(s) sent, {skipped} unavailable, {failed} failed."
            for label, names in (("Unavailable", skipped_names), ("Failed", failed_names)):
                if names:
                    summary += f"\n{label}: {', '.join(names[:8])}"
                    if len(names) > 8:
                        summary += f" and {len(names) - 8} more"
            for name, reason in failure_details:
                summary += f"\n{name}: {reason}"
            if repeated_failures:
                summary += "\nThe website failed repeatedly; try again later."
            if session_failed:
                summary += "\nThe browser session failed; try again later."
            if stopped or failed:
                summary += "\nUse the language buttons to retry."
            await self._finish_progress(progress, job.chat_id, summary)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if progress is not None:
                await progress.close()
            self._untrack_cancel(self._batch_cancellations, job.chat_id, cancel)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram update handling failed (%s)", type(context.error).__name__)
