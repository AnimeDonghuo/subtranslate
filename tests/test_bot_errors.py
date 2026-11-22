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
from subtranslate.subtitles import TranslatedSubtitle
from subtranslate.website import WebsiteError

VALID_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
JOB = TranslationJob(42, 77, "file-id", "movie.srt", len(VALID_SRT), "hi", "Hindi")


@pytest.fixture
def service():
    settings = Settings("12345:TESTTOKEN", "https://example.com")
    translator = SimpleNamespace(translate=AsyncMock())
    result = BotService(settings, LanguageCatalog(), translator)
    result.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        edit_message_text=AsyncMock(), get_file=AsyncMock(), send_document=AsyncMock(),
    )
    return result


def telegram_file(data: bytes, size: int | None = None):
    return SimpleNamespace(
        file_size=len(data) if size is None else size,
        download_as_bytearray=AsyncMock(return_value=bytearray(data)),
    )


def last_notice(service: BotService) -> str:
    return service.bot.edit_message_text.await_args.kwargs["text"]


async def test_invalid_srt_is_rejected_before_upload_to_site(service):
    service.bot.get_file.return_value = telegram_file(b"not a subtitle")
    await service.process_job(JOB)
    assert "Could not translate:" in last_notice(service)
    service.translator.translate.assert_not_awaited()
    service.bot.send_document.assert_not_awaited()


async def test_checks_downloaded_size_even_if_metadata_claims_it_is_small(service):
    service.bot.get_file.return_value = telegram_file(b"x" * 2048, size=10)
    await service.process_job(JOB)
    assert "incomplete" in last_notice(service)
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
        "12345:TESTTOKEN", "https://example.com", translation_timeout_seconds=0.2,
    )
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    cancelled = asyncio.Event()

    async def slow_translate(source, code, *, on_progress=None):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.translator.translate.side_effect = slow_translate
    await service.process_job(JOB)
    assert cancelled.is_set()
    assert "timed out" in last_notice(service)
    service.bot.send_document.assert_not_awaited()


@pytest.mark.parametrize("stage", [
    "starting the browser", "uploading and parsing the subtitle", "translating",
    "waiting for translation to settle", "downloading", "validating the downloaded file",
])
async def test_large_or_active_stages_have_no_fixed_total_deadline(service, stage):
    service.settings = Settings("12345:TESTTOKEN", "https://example.com", translation_timeout_seconds=0.05)
    service.bot.get_file.return_value = telegram_file(VALID_SRT)

    async def slow_stage(source, code, *, on_progress=None):
        on_progress(stage)
        await asyncio.sleep(0.1)
        return TranslatedSubtitle(VALID_SRT, 1, unchanged=False)

    service.translator.translate.side_effect = slow_stage
    await service.process_job(JOB)
    service.bot.send_document.assert_awaited_once()


async def test_cancel_single_during_delivery_cleans_up_without_claiming_completion(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    started = asyncio.Event()
    canceled = asyncio.Event()
    paths = []

    async def translate(source, code, *, on_progress=None):
        paths.append(source.path)
        return TranslatedSubtitle(source.path, 1, unchanged=False, changed_cues=1)

    async def slow_send(**kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            canceled.set()

    service.translator.translate.side_effect = translate
    service.bot.send_document.side_effect = slow_send
    task = asyncio.create_task(service.process_job(JOB))
    await asyncio.wait_for(started.wait(), 2)
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()),
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=SimpleNamespace(id=42))
    await service.cancel_batch(update, None)
    await asyncio.wait_for(task, 3)
    assert canceled.is_set() and not paths[0].exists() and not paths[0].parent.exists()
    assert "canceled" in last_notice(service).lower()
    assert not service._single_cancellations


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
    assert send["document"].filename == "Hindi.srt"


async def test_partly_unchanged_file_is_sent_with_the_cue_range_that_did_not_change(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.return_value = TranslatedSubtitle(
        VALID_SRT, 100, unchanged=False, changed_cues=60, longest_unchanged_run=40,
        longest_run_start=60, unchanged_cues=40,
    )
    await service.process_job(JOB)

    send = service.bot.send_document.await_args.kwargs
    assert send["document"].filename == "Hindi.srt"
    assert "40 cues in a row" in send["caption"]
    assert "cue 61–100" in send["caption"] and "40 of 100 cues (40%)" in send["caption"]
    assert "Please check it" in send["caption"]


async def test_source_script_stretch_is_flagged_even_when_cue_text_changed(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.return_value = TranslatedSubtitle(
        VALID_SRT, 100, unchanged=False, changed_cues=50, untranslated_run=12,
    )
    await service.process_job(JOB)

    caption = service.bot.send_document.await_args.kwargs["caption"]
    assert "12 cues in a row still look like the original language" in caption


async def test_caption_reports_changed_text_as_a_comparison_not_a_translation_guarantee(service):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.return_value = TranslatedSubtitle(VALID_SRT, 2, unchanged=False, changed_cues=1)
    await service.process_job(JOB)
    caption = service.bot.send_document.await_args.kwargs["caption"]
    assert "2 cues checked" in caption and "1/2 cue texts differ" in caption
    # The delivered file is named after the language, and the caption says so.
    assert "📄 Hindi.srt" in caption
    assert "may stay the same" in caption and "Please review" in caption


async def test_status_edit_failure_does_not_discard_a_translated_file(service, caplog):
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.bot.edit_message_text.side_effect = TelegramError("DO_NOT_LOG_THIS_TOKEN")
    service.translator.translate.return_value = TranslatedSubtitle(VALID_SRT, 1, unchanged=False)
    await service.process_job(JOB)
    service.bot.send_document.assert_awaited_once()
    assert "Hindi complete" in service.bot.send_message.await_args.kwargs["text"]
    assert "DO_NOT_LOG_THIS_TOKEN" not in caplog.text


async def test_job_does_not_run_when_status_cannot_be_delivered(service):
    service.bot.send_message.side_effect = TelegramError("user blocked the bot")
    await service.process_job(JOB)
    service.bot.get_file.assert_not_awaited()
    service.translator.translate.assert_not_awaited()


async def test_possible_partial_result_is_delivered_but_never_as_complete(service):
    # Refusing the job used to leave the user with nothing; the file is sent
    # and the status says plainly that part of it may be untranslated.
    service.bot.get_file.return_value = telegram_file(VALID_SRT)
    service.translator.translate.return_value = TranslatedSubtitle(
        VALID_SRT, 50, unchanged=False, changed_cues=5, longest_unchanged_run=45,
    )
    await service.process_job(JOB)

    service.bot.send_document.assert_awaited_once()
    assert "some text may be untranslated" in last_notice(service)
    assert "Please review the file" in last_notice(service)


async def test_a_failed_translation_is_forwarded_to_the_log_channel_with_the_reason(monkeypatch):
    channel = -1009876543210
    settings = Settings("12345:TESTTOKEN", "https://example.com", log_channel_id=channel)
    translator = SimpleNamespace(translate=AsyncMock(side_effect=WebsiteError("The website timed out.")))
    service = BotService(settings, LanguageCatalog(), translator)
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        edit_message_text=AsyncMock(), send_document=AsyncMock(),
        get_file=AsyncMock(return_value=telegram_file(VALID_SRT)),
    )

    await service.process_job(JOB)

    # One message to the channel: the failure plus the user's own file.
    copy = service.bot.send_document.await_args
    assert copy.kwargs["chat_id"] == channel
    assert "Translation failed" in copy.kwargs["caption"]
    assert "The website timed out." in copy.kwargs["caption"]
    assert "movie.srt" in copy.kwargs["caption"]
    assert "Hindi" in copy.kwargs["caption"]
    # The user still gets the normal failure notice.
    assert "Could not translate" in last_notice(service)


async def test_a_canceled_job_is_not_reported_as_a_failure():
    channel = -1009876543210
    settings = Settings("12345:TESTTOKEN", "https://example.com", log_channel_id=channel)

    async def forever(*args, **kwargs):
        await asyncio.Event().wait()

    translator = SimpleNamespace(translate=AsyncMock(side_effect=forever))
    service = BotService(settings, LanguageCatalog(), translator)
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        edit_message_text=AsyncMock(), send_document=AsyncMock(),
        get_file=AsyncMock(return_value=telegram_file(VALID_SRT)),
    )
    task = asyncio.create_task(service.process_job(JOB))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if service.translator.translate.await_count:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    service.bot.send_document.assert_not_awaited()
