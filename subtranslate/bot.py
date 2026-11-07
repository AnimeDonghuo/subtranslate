"""Telegram interaction and job processing."""

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
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

from .config import Settings
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
from .progress import ProgressMessage
from .proxies import parse_proxy_pool
from .subtitles import (
    MAX_INPUT_BYTES,
    SubtitleError,
    prepare_subtitle,
    subtitle_extension,
    translated_filename,
)
from .website import LanguageUnavailable, WebsiteError, WebsiteTranslator

logger = logging.getLogger(__name__)

HELP = (
    "👋 Send a text subtitle file (SRT, ASS, SUB, SBV, VTT or Spruce STL). "
    "SRT files have no bot-wide size or cue-count cap; large jobs depend on Telegram, disk space and the website. "
    "Other formats are limited to 1 MB and 3,000 cues. "
    "Binary VobSub and EBU STL are not supported. Choose a language, or select all to translate one language at a time. "
    "I’ll use translatesubtitles.co and send each file back with the language in its name.\n\n"
    "The site uses Google Translate; please review the result. Your subtitle text is sent to "
    "the site and Google, so don't send confidential files. You need permission to translate the file.\n\n"
    "Use this bot in a private chat. Select all may take hours; use /cancel to stop a batch. "
    "If a job is interrupted, tap the language button again. "
    "Allowed users can use /proxychk to configure website proxies."
)
PICK = "Choose the language you want for this subtitle:"
MAX_PROXY_INPUT_BYTES = 16 * 1024


class JobCancelled(Exception):
    """A user canceled the active translation or transfer."""


class BotService:
    def __init__(self, settings: Settings, catalog: LanguageCatalog, translator: WebsiteTranslator) -> None:
        self.settings = settings
        self.catalog = catalog
        self.translator = translator
        self.queue = JobQueue(self.process_job, max_waiting=2)
        self._batch_cancellations: dict[int, asyncio.Event] = {}
        self._single_cancellations: dict[int, asyncio.Event] = {}
        self._proxy_input_chats: set[int] = set()
        self.bot = None

    def install_handlers(self, application) -> None:
        self.bot = application.bot
        application.add_handler(CommandHandler(["start", "help"], self.help))
        application.add_handler(CommandHandler("id", self.user_id))
        application.add_handler(CommandHandler("cancel", self.cancel_batch))
        application.add_handler(CommandHandler("proxychk", self.proxy_command))
        application.add_handler(MessageHandler(filters.Document.ALL, self.document))
        application.add_handler(CallbackQueryHandler(self.callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.other_text))
        application.add_error_handler(self.on_error)

    async def shutdown(self) -> None:
        """Best-effort notices for jobs interrupted by a graceful shutdown."""
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

    def _permitted(self, user_id: int | None) -> bool:
        return bool(user_id) and (self.settings.allowed_user_ids is None or user_id in self.settings.allowed_user_ids)

    async def _is_private_and_allowed(self, update: Update) -> bool:
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not message or not chat or not user:
            return False
        if chat.type != "private":
            await message.reply_text("Please send subtitle files to me in a private chat.")
            return False
        if not self._permitted(user.id):
            await message.reply_text("This bot is restricted to its owner.")
            return False
        return True

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._is_private_and_allowed(update):
            await update.effective_message.reply_text(HELP)

    async def user_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Allow an owner to discover their ID before configuring the allowlist.
        if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
            await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")

    async def cancel_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        chat_id = update.effective_chat.id
        removed = self.queue.cancel_waiting_jobs(chat_id)
        active = self._batch_cancellations.get(chat_id) or self._single_cancellations.get(chat_id)
        if active:
            active.set()
        if active or removed:
            await update.effective_message.reply_text(
                "Canceled queued jobs. Active work is stopping; no unfinished subtitle will be sent."
            )
        else:
            await update.effective_message.reply_text("You have no active or queued translation job.")

    async def _proxy_admin_allowed(self, update: Update) -> bool:
        if not await self._is_private_and_allowed(update):
            return False
        # Proxy configuration affects every user's traffic. Never let an
        # unrestricted public bot give arbitrary users that control.
        if self.settings.allowed_user_ids is None:
            await update.effective_message.reply_text(
                "Set ALLOWED_USER_IDS on the deployment before using /proxychk. "
                "Every listed user can change the bot-wide proxy pool."
            )
            return False
        return True

    async def proxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._proxy_admin_allowed(update):
            return
        chat_id = update.effective_chat.id
        args = getattr(context, "args", None) or []
        if args and len(args) == 1 and args[0].lower() == "direct":
            self._proxy_input_chats.discard(chat_id)
            await self._apply_proxy_input(update, "direct")
        elif args and len(args) == 1 and args[0].lower() == "cancel":
            self._proxy_input_chats.discard(chat_id)
            await update.effective_message.reply_text("Proxy input canceled; the existing configuration is unchanged.")
        elif args:
            await update.effective_message.reply_text("Use /proxychk, /proxychk direct, or /proxychk cancel.")
        else:
            self._proxy_input_chats.add(chat_id)
            await update.effective_message.reply_text(
                "Send up to 10 HTTP, HTTPS, SOCKS4, SOCKS5 or VLESS links (or HTTP host:port) as text or a UTF-8 file "
                "(one per line or comma-separated). Send direct to disable proxies, or /proxychk cancel. "
                "Only common VLESS transports are supported. "
                "This changes the bot-wide pool; proxy credentials sent in Telegram are not end-to-end encrypted."
            )

    async def _apply_proxy_input(self, update: Update, value: str) -> None:
        message = update.effective_message
        if len(value.encode("utf-8")) > MAX_PROXY_INPUT_BYTES:
            await message.reply_text("Proxy input is too large (maximum 16 KiB). Use /proxychk to try again.")
            return
        if value.strip().lower() == "direct":
            proxies = ()
        else:
            entries = [part.strip() for line in value.splitlines() for part in line.split(",") if part.strip()]
            try:
                proxies = parse_proxy_pool(",".join(entries))
                if not proxies:
                    raise ValueError
            except ValueError:
                await message.reply_text(
                    "Invalid or unsupported proxy list. Use HTTP(S), unauthenticated SOCKS4/5, "
                    "or a supported VLESS link. The existing pool is unchanged; use /proxychk to retry."
                )
                return
        await message.reply_text("Waiting for any active batch to finish, then checking proxy settings. This may take time.")
        try:
            count = await self.translator.configure_proxies(proxies)
        except WebsiteError:
            await message.reply_text("Could not check the proxies. The existing configuration is unchanged.")
            return
        if not proxies:
            await message.reply_text("Direct server connection enabled for future translations.")
        elif count:
            await message.reply_text(
                f"{count} of {len(proxies)} proxies opened the upload page and are now in the bot-wide pool. "
                "This does not guarantee translation will work."
            )
        else:
            await message.reply_text("No proxies passed the website check; the existing configuration is unchanged.")

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
                await message.reply_text("Send a non-empty UTF-8 proxy file of at most 16 KiB. Use /proxychk to retry.")
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
        # Explicitly reply to the original document. Telegram includes it in
        # callback messages, so a language selection survives Koyeb sleeping.
        markup, page, pages = language_keyboard(self.catalog)
        await message.reply_text(
            f"{PICK}\n\nPage {page}/{pages}",
            reply_markup=markup,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False),
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        message = query.message
        user = query.from_user
        if not isinstance(message, Message) or message.chat.type != "private" or message.chat.id != user.id:
            await query.answer("Please use the bot in a private chat.", show_alert=True)
            return
        if not self._permitted(user.id):
            await query.answer("This bot is restricted to its owner.", show_alert=True)
            return
        data = query.data or ""
        if data.startswith("page:"):
            try:
                page_number = int(data.removeprefix("page:"))
                markup, current, total = language_keyboard(self.catalog, page_number)
            except ValueError:
                await query.answer("Please choose a language again.", show_alert=True)
                return
            await query.answer()
            await self._edit_picker(query, f"{PICK}\n\nPage {current}/{total}", markup)
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
            languages = tuple(self.catalog.items())
            code, name = "*", "all languages"
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
        )
        status = self.queue.submit(job)
        if status is EnqueueResult.DUPLICATE:
            await query.answer("That translation is already queued or running.")
        elif status is EnqueueResult.FULL:
            await query.answer("The queue is full. Please tap again after a job finishes.", show_alert=True)
        else:
            await query.answer(f"Queued for {name}")
            if languages:
                await message.reply_text(
                    f"Queued {len(languages)} languages. A stage bar will appear when the batch starts. "
                    "I’ll send each file one by one. This may take hours; use /cancel to stop."
                )
            else:
                await message.reply_text(
                    f"Queued for {name}. A stage bar will appear when the job starts. "
                    "This may take several minutes on the free instance."
                )

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
        if job.languages:
            await self._process_all(job)
            return
        cancel = asyncio.Event()
        self._single_cancellations[job.chat_id] = cancel
        try:
            progress = await ProgressMessage.start(self.bot, job.chat_id, job.language_name)
        except BaseException:
            self._single_cancellations.pop(job.chat_id, None)
            raise
        if progress is None:
            self._single_cancellations.pop(job.chat_id, None)
            return
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
            self._single_cancellations.pop(job.chat_id, None)

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
        self._batch_cancellations[job.chat_id] = cancel
        heartbeat = None
        progress = None
        delivered = skipped = failed = consecutive_failures = 0
        skipped_names: list[str] = []
        failed_names: list[str] = []
        failure_details: list[tuple[str, str]] = []
        stopped = session_failed = repeated_failures = False
        try:
            progress = await ProgressMessage.start(self.bot, job.chat_id, "all languages", batch_total=len(job.languages))
            if progress is None:
                return
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
            self._batch_cancellations.pop(job.chat_id, None)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram update handling failed (%s)", type(context.error).__name__)
