"""The bot should report failures without sending malformed subtitles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.jobs import TranslationJob
from subtranslate.languages import LanguageCatalog
from subtranslate.subtitles import MAX_INPUT_BYTES, TranslatedSubtitle
from subtranslate.website import WebsiteError

VALID_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
JOB = TranslationJob(42, 77, "file-id", "movie.srt", len(VALID_SRT), "hi", "Hindi")


@pytest.fixture
def service():
    settings = Settings("12345:TESTTOKEN", "long_secret_token_123", "https://example.com")
    translator = SimpleNamespace(translate=AsyncMock())
    result = BotService(settings, LanguageCatalog(), translator)
    result.bot = SimpleNamespace(
        send_message=AsyncMock(), get_file=AsyncMock(), send_document=AsyncMock(),
    )
    return result


def telegram_file(data: bytes, size: int | None = None):
    return SimpleNamespace(
        file_size=len(data) if size is None else size,
        download_as_bytearray=AsyncMock(return_value=bytearray(data)),
    )


def last_notice(service: BotService) -> str:
    return service.bot.send_message.await_args.kwargs["text"]


async def test_invalid_srt_is_rejected_before_upload_to_site(service):
    service.bot.get_file.return_value = telegram_file(b"not a subtitle")
    await service.process_job(JOB)
    assert "Could not translate:" in last_notice(service)
    service.translator.translate.assert_not_awaited()
    service.bot.send_document.assert_not_awaited()


async def test_checks_downloaded_size_even_if_metadata_claims_it_is_small(service):
    service.bot.get_file.return_value = telegram_file(b"x" * (MAX_INPUT_BYTES + 1), size=10)
    await service.process_job(JOB)
    assert "too large" in last_notice(service)
    service.translator.translate.assert_not_awaited()
    service.bot.send_document.assert_not_awaited()


async def test_website_failure_is_explained_to_user(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.side_effect = WebsiteError("The website is unavailable.")
    await service.process_job(JOB)
    assert "The website is unavailable" in last_notice(service)
    service.bot.send_document.assert_not_awaited()


async def test_timeout_is_reported_and_browser_coroutine_cancelled(service):
    service.settings = Settings(
        "12345:TESTTOKEN", "long_secret_token_123", "https://example.com", translation_timeout_seconds=0.2,
    )
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    cancelled = asyncio.Event()

    async def slow_translate(source, code):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.translator.translate.side_effect = slow_translate
    await service.process_job(JOB)
    assert cancelled.is_set()
    assert "timed out" in last_notice(service)
    service.bot.send_document.assert_not_awaited()


async def test_telegram_transfer_error_is_reported(service):
    service.bot.get_file.side_effect = TelegramError("transfer failed")
    await service.process_job(JOB)
    assert "Telegram could not" in last_notice(service)
    service.translator.translate.assert_not_awaited()


async def test_unchanged_file_has_a_warning_in_the_caption(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.return_value = TranslatedSubtitle(VALID_SRT, 1, unchanged=True)
    await service.process_job(JOB)
    send = service.bot.send_document.await_args.kwargs
    assert "unchanged text" in send["caption"]
    assert send["document"].filename == "movie.Hindi.srt"


async def test_job_does_not_run_when_status_cannot_be_delivered(service):
    service.bot.send_message.side_effect = TelegramError("user blocked the bot")
    await service.process_job(JOB)
    service.bot.get_file.assert_not_awaited()
    service.translator.translate.assert_not_awaited()
