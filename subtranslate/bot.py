"""Telegram interaction and job processing."""

import asyncio
import logging
from io import BytesIO

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
from .languages import (
    LanguageCatalog,
    group_keyboard,
    letters_keyboard,
    popular_keyboard,
)
from .subtitles import (
    MAX_INPUT_BYTES,
    SubtitleError,
    prepare_subtitle,
    translated_filename,
)
from .website import WebsiteError, WebsiteTranslator

logger = logging.getLogger(__name__)

HELP = (
    "👋 Send me an .srt subtitle file (up to 1 MB and 3,000 cues), then choose a target language. "
    "I’ll translate it with translatesubtitles.co and send the SRT back with the language in its name.\n\n"
    "The site uses Google Translate; please review the result. Your subtitle text is sent to "
    "the site and Google, so don't send confidential files. You need permission to translate the file.\n\n"
    "Use this bot in a private chat. If a job is interrupted, tap the language button again."
)
PICK = "Choose the language you want for this subtitle:"


class BotService:
    def __init__(self, settings: Settings, catalog: LanguageCatalog, translator: WebsiteTranslator) -> None:
        self.settings = settings
        self.catalog = catalog
        self.translator = translator
        self.queue = JobQueue(self.process_job, max_waiting=2)
        self.bot = None

    def install_handlers(self, application) -> None:
        self.bot = application.bot
        application.add_handler(CommandHandler(["start", "help"], self.help))
        application.add_handler(CommandHandler("id", self.user_id))
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

    async def other_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._is_private_and_allowed(update):
            await update.effective_message.reply_text("Send an .srt file as a document, then choose a language. /help")

    async def document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_private_and_allowed(update):
            return
        message = update.effective_message
        doc = message.document
        if not doc or not doc.file_name or not doc.file_name.lower().endswith(".srt"):
            await message.reply_text("Please send a subtitle file with the .srt extension.")
            return
        if doc.file_size is not None and (doc.file_size <= 0 or doc.file_size > MAX_INPUT_BYTES):
            await message.reply_text("Please send a non-empty SRT up to 1,000,000 bytes (about 1 MB).")
            return
        # Explicitly reply to the original document. Telegram includes it in
        # callback messages, so a language selection survives Koyeb sleeping.
        await message.reply_text(
            PICK,
            reply_markup=popular_keyboard(self.catalog),
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
        if data == "popular":
            await query.answer()
            await self._edit_picker(query, PICK, popular_keyboard(self.catalog))
            return
        if data == "letters":
            await query.answer()
            await self._edit_picker(query, PICK + "\n\nChoose the first letter:", letters_keyboard(self.catalog))
            return
        if data.startswith("group:"):
            try:
                _, letter, page = data.split(":")
                markup, current, total = group_keyboard(self.catalog, letter, int(page))
            except (TypeError, ValueError):
                await query.answer("Please choose a language again.", show_alert=True)
                return
            await query.answer()
            await self._edit_picker(query, f"{PICK}\n\n{letter} · page {current}/{total}", markup)
            return
        if not data.startswith("lang:"):
            await query.answer("Please choose a language again.", show_alert=True)
            return
        code = data.removeprefix("lang:")
        name = self.catalog.get(code)
        original = message.reply_to_message
        if not name:
            await query.answer("This language is no longer in the list. Please resend the file.", show_alert=True)
            return
        if not original or not original.document or not original.from_user or original.from_user.id != user.id:
            await query.answer("The original file is no longer available. Please send it again.", show_alert=True)
            return
        doc = original.document
        if not doc.file_name or not doc.file_name.lower().endswith(".srt"):
            await query.answer("Please send an .srt file again.", show_alert=True)
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
        )
        status = self.queue.submit(job)
        if status is EnqueueResult.DUPLICATE:
            await query.answer("That translation is already queued or running.")
        elif status is EnqueueResult.FULL:
            await query.answer("The queue is full. Please tap again after a job finishes.", show_alert=True)
        else:
            await query.answer(f"Queued for {name}")
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

    async def process_job(self, job: TranslationJob) -> None:
        if not await self._notice(job.chat_id, f"Translating to {job.language_name} now…"):
            return
        try:
            async with asyncio.timeout(self.settings.translation_timeout_seconds):
                telegram_file = await self.bot.get_file(job.file_id)
                file_size = telegram_file.file_size if telegram_file.file_size is not None else job.file_size
                if file_size is None or file_size <= 0 or file_size > MAX_INPUT_BYTES:
                    raise SubtitleError("The file is empty, too large, or its size could not be verified. Please resend an SRT up to 1 MB.")
                data = await telegram_file.download_as_bytearray(read_timeout=60)
                if len(data) > MAX_INPUT_BYTES:
                    raise SubtitleError("The SRT is too large. Please send a file up to 1 MB.")
                source = await asyncio.to_thread(prepare_subtitle, bytes(data))
                result = await self.translator.translate(source, job.language_code)
                filename = translated_filename(job.file_name, job.language_name)
                caption = f"{job.language_name} · {result.cue_count:,} cues\nPlease review the machine translation."
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
        except (SubtitleError, WebsiteError) as exc:
            await self._notice(job.chat_id, f"Could not translate: {exc}\n\nYou can tap a language button to try again.")
        except TimeoutError:
            await self._notice(job.chat_id, "Translation timed out. Try again or use a smaller SRT.")
        except TelegramError as exc:
            logger.warning("Telegram file transfer failed (%s)", type(exc).__name__)
            await self._notice(job.chat_id, "Telegram could not download or send the file. Please resend the SRT and try again.")
        except Exception as exc:  # noqa: BLE001 - notify the user on unexpected job failures
            logger.error("Translation failed (%s)", type(exc).__name__)
            await self._notice(job.chat_id, "Something went wrong while translating. Please try again later.")

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram update handling failed (%s)", type(context.error).__name__)
