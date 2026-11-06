"""Telegram interaction and job processing."""

import asyncio
import logging
from io import BytesIO

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
from .jobs import EnqueueResult, JobQueue, TranslationJob
from .languages import LanguageCatalog, confirm_all_keyboard, language_keyboard
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
    "👋 Send a text subtitle file (SRT, ASS, SUB, SBV, VTT or Spruce STL; up to 1 MB and 3,000 cues). "
    "Binary VobSub and EBU STL are not supported. Choose a language, or select all to translate one language at a time. "
    "I’ll use translatesubtitles.co and send each file back with the language in its name.\n\n"
    "The site uses Google Translate; please review the result. Your subtitle text is sent to "
    "the site and Google, so don't send confidential files. You need permission to translate the file.\n\n"
    "Use this bot in a private chat. Select all may take hours; use /cancel to stop a batch. "
    "If a job is interrupted, tap the language button again."
)
PICK = "Choose the language you want for this subtitle:"


class BotService:
    def __init__(self, settings: Settings, catalog: LanguageCatalog, translator: WebsiteTranslator) -> None:
        self.settings = settings
        self.catalog = catalog
        self.translator = translator
        self.queue = JobQueue(self.process_job, max_waiting=2)
        self._batch_cancellations: dict[int, asyncio.Event] = {}
        self.bot = None

    def install_handlers(self, application) -> None:
        self.bot = application.bot
        application.add_handler(CommandHandler(["start", "help"], self.help))
        application.add_handler(CommandHandler("id", self.user_id))
        application.add_handler(CommandHandler("cancel", self.cancel_batch))
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
        removed = self.queue.cancel_waiting_batches(chat_id)
        active = self._batch_cancellations.get(chat_id)
        if active:
            active.set()
        if active or removed:
            await update.effective_message.reply_text(
                "Canceled queued all-language jobs. An active batch will stop after the current language."
            )
        else:
            await update.effective_message.reply_text("You have no active or queued all-language batch.")

    async def other_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._is_private_and_allowed(update):
            await update.effective_message.reply_text("Send a subtitle file as a document, then choose a language. /help")

    async def document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        message = update.effective_message
        doc = message.document
        try:
            subtitle_extension(doc.file_name if doc and doc.file_name else "")
        except SubtitleError as exc:
            await message.reply_text(str(exc))
            return
        if doc.file_size is not None and (doc.file_size <= 0 or doc.file_size > MAX_INPUT_BYTES):
            await message.reply_text("Please send a non-empty subtitle up to 1,000,000 bytes (about 1 MB).")
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
            subtitle_extension(doc.file_name or "")
        except SubtitleError:
            await query.answer("Please send a supported subtitle file again.", show_alert=True)
            return
        if doc.file_size is not None and (doc.file_size <= 0 or doc.file_size > MAX_INPUT_BYTES):
            await query.answer("The file must be non-empty and at most 1 MB.", show_alert=True)
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
                    f"Queued {len(languages)} languages. I’ll send each file one by one. "
                    "This may take hours; use /cancel to stop."
                )
            else:
                await message.reply_text(
                    f"Queued for {name}. I’ll send the file here when it’s ready. "
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

    async def _download_source(self, job: TranslationJob):
        telegram_file = await self.bot.get_file(job.file_id)
        file_size = telegram_file.file_size if telegram_file.file_size is not None else job.file_size
        if file_size is None or file_size <= 0 or file_size > MAX_INPUT_BYTES:
            raise SubtitleError("The file is empty, too large, or its size could not be verified. Please resend a subtitle up to 1 MB.")
        data = await telegram_file.download_as_bytearray(read_timeout=60)
        if len(data) > MAX_INPUT_BYTES:
            raise SubtitleError("The subtitle is too large. Please send a file up to 1 MB.")
        return await asyncio.to_thread(prepare_subtitle, bytes(data), job.file_name)

    async def _send_result(self, job: TranslationJob, name: str, result, progress: str = "") -> None:
        filename = translated_filename(job.file_name, name)
        caption = f"{progress}{name} · {result.cue_count:,} cues\nPlease review the machine translation."
        if result.unchanged:
            caption += (
                "\n\n⚠️ The website returned unchanged text. It may already be in the target "
                "language, or translation may have failed."
            )
        await self.bot.send_document(
            chat_id=job.chat_id,
            document=InputFile(BytesIO(result.content), filename=filename),
            caption=caption,
            reply_parameters=ReplyParameters(message_id=job.original_message_id, allow_sending_without_reply=True),
            write_timeout=120,
            read_timeout=120,
        )

    async def process_job(self, job: TranslationJob) -> None:
        if job.languages:
            await self._process_all(job)
            return
        if not await self._notice(job.chat_id, f"Translating to {job.language_name} now…"):
            return
        try:
            async with asyncio.timeout(self.settings.translation_timeout_seconds):
                source = await self._download_source(job)
                result = await self.translator.translate(source, job.language_code)
                await self._send_result(job, job.language_name, result)
        except (SubtitleError, WebsiteError) as exc:
            await self._notice(job.chat_id, f"Could not translate: {exc}\n\nYou can tap a language button to try again.")
        except TimeoutError:
            await self._notice(job.chat_id, "Translation timed out. Try again or use a smaller subtitle.")
        except TelegramError as exc:
            logger.warning("Telegram file transfer failed (%s)", type(exc).__name__)
            await self._notice(job.chat_id, "Telegram could not download or send the file. Please resend it and try again.")
        except Exception as exc:  # noqa: BLE001 - notify the user on unexpected job failures
            logger.error("Translation failed (%s)", type(exc).__name__)
            await self._notice(job.chat_id, "Something went wrong while translating. Please try again later.")

    async def _keep_active_batch_awake(self) -> None:
        # A batch is real work, but Koyeb only sees incoming HTTP traffic. Ping
        # the public health route while it runs; stop immediately when it ends.
        # This is best effort and does not make the in-memory batch durable.
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(20 * 60)
                try:
                    async with session.get(self.settings.webhook_base_url + "/healthz") as response:
                        await response.read()
                except (aiohttp.ClientError, TimeoutError) as exc:
                    logger.info("Batch heartbeat unavailable (%s)", type(exc).__name__)

    async def _process_all(self, job: TranslationJob) -> None:
        cancel = asyncio.Event()
        self._batch_cancellations[job.chat_id] = cancel
        heartbeat = None
        delivered = skipped = failed = consecutive_failures = 0
        skipped_names: list[str] = []
        failed_names: list[str] = []
        stopped = False
        try:
            if not await self._notice(
                job.chat_id, f"Starting {len(job.languages)} languages, one at a time. Use /cancel to stop."
            ):
                return
            heartbeat = asyncio.create_task(self._keep_active_batch_awake(), name="batch-heartbeat")
            try:
                async with asyncio.timeout(self.settings.translation_timeout_seconds):
                    source = await self._download_source(job)
            except Exception as exc:  # noqa: BLE001 - notify the user if the batch cannot start
                logger.warning("Could not start all-language batch (%s)", type(exc).__name__)
                await self._notice(job.chat_id, "Could not read the subtitle file. Please resend it and try again.")
                return
            for number, (code, name) in enumerate(job.languages, start=1):
                if cancel.is_set():
                    stopped = True
                    break
                try:
                    async with asyncio.timeout(self.settings.translation_timeout_seconds):
                        result = await self.translator.translate(source, code)
                        await self._send_result(job, name, result, progress=f"{number}/{len(job.languages)} · ")
                    delivered += 1
                    consecutive_failures = 0
                except LanguageUnavailable:
                    skipped += 1
                    skipped_names.append(name)
                    # A stale catalog entry is not a website outage.
                    consecutive_failures = 0
                except TelegramError as exc:
                    failed += 1
                    failed_names.append(name)
                    stopped = True
                    logger.warning("Telegram batch delivery failed (%s)", type(exc).__name__)
                    break
                except Exception as exc:  # noqa: BLE001 - prevent repeated failures from running all day
                    failed += 1
                    failed_names.append(name)
                    consecutive_failures += 1
                    logger.warning("Batch language failed (%s)", type(exc).__name__)
                    if consecutive_failures >= 3:
                        stopped = True
                        await self._notice(job.chat_id, "The website failed repeatedly; stopping the batch. Try again later.")
                        break
            if cancel.is_set():
                stopped = True
            suffix = "Stopped" if stopped else "Finished"
            summary = f"{suffix}: {delivered} file(s) sent, {skipped} unavailable, {failed} failed."
            for label, names in (("Unavailable", skipped_names), ("Failed", failed_names)):
                if names:
                    summary += f"\n{label}: {', '.join(names[:8])}"
                    if len(names) > 8:
                        summary += f" and {len(names) - 8} more"
            if stopped or failed:
                summary += "\nUse the language buttons to retry."
            await self._notice(job.chat_id, summary)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self._batch_cancellations.pop(job.chat_id, None)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram update handling failed (%s)", type(context.error).__name__)
