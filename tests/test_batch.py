import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.jobs import EnqueueResult, JobQueue, TranslationJob
from subtranslate.languages import LanguageCatalog
from subtranslate.large_srt import FileSubtitle
from subtranslate.subtitles import TranslatedSubtitle
from subtranslate.website import LanguageUnavailable, WebsiteError

SOURCE = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
LANGUAGES = (("hi", "Hindi"), ("es", "Spanish"), ("fr", "French"))
JOB = TranslationJob(42, 77, "file", "movie.srt", len(SOURCE), "*", "all languages", LANGUAGES)


@pytest.fixture
def batch_service():
    telegram_file = SimpleNamespace(file_size=len(SOURCE), download_as_bytearray=AsyncMock(return_value=bytearray(SOURCE)))
    settings = Settings("12345:TESTTOKEN", "https://example.com")
    translator = SimpleNamespace(translate=AsyncMock())

    @asynccontextmanager
    async def session():
        yield translator

    translator.session = session
    service = BotService(settings, LanguageCatalog(), translator)
    service.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
                                  edit_message_text=AsyncMock(), get_file=AsyncMock(return_value=telegram_file),
                                  send_document=AsyncMock())
    return service


def fake_result(source):
    if isinstance(source, FileSubtitle):
        return TranslatedSubtitle(source.path.read_bytes(), source.cue_count, False)
    return TranslatedSubtitle(source.utf8, len(source.cues), False)


def cancel_update():
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=42, type="private"),
                           effective_user=SimpleNamespace(id=42))


async def test_all_languages_are_sent_one_by_one(batch_service):
    seen = []
    active = peak = 0

    async def translate(source, code, *, on_progress=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(code)
        await asyncio.sleep(0)
        active -= 1
        return fake_result(source)

    batch_service.translator.translate.side_effect = translate
    await batch_service.process_job(JOB)
    assert seen == ["hi", "es", "fr"] and peak == 1
    batch_service.bot.get_file.assert_awaited_once()
    sends = batch_service.bot.send_document.await_args_list
    assert [item.kwargs["document"].filename for item in sends] == [
        "movie.Hindi.srt", "movie.Spanish.srt", "movie.French.srt",
    ]
    assert [item.kwargs["caption"].split(" · ")[0] for item in sends] == ["1/3", "2/3", "3/3"]
    assert "3 file(s) sent" in batch_service.bot.edit_message_text.await_args.kwargs["text"]


async def test_batch_skips_missing_language_and_continues(batch_service):
    async def translate(source, code, *, on_progress=None):
        if code == "es":
            raise LanguageUnavailable("Unavailable")
        return fake_result(source)

    batch_service.translator.translate.side_effect = translate
    await batch_service.process_job(JOB)
    assert batch_service.bot.send_document.await_count == 2
    summary = batch_service.bot.edit_message_text.await_args.kwargs["text"]
    assert "1 unavailable" in summary and "Unavailable: Spanish" in summary


async def test_batch_stops_after_repeated_site_failure(batch_service):
    extra = LANGUAGES + (("de", "German"),)
    batch_service.translator.translate.side_effect = WebsiteError("site unavailable")
    await batch_service.process_job(replace(JOB, languages=extra))
    assert batch_service.translator.translate.await_count == 3
    batch_service.bot.send_document.assert_not_awaited()
    summary = batch_service.bot.edit_message_text.await_args.kwargs["text"]
    assert "Stopped" in summary and "Failed: Hindi, Spanish, French" in summary


async def test_batch_reports_a_rejected_incomplete_download_and_continues(batch_service):
    async def translate(source, code, *, on_progress=None):
        if code == "hi":
            raise WebsiteError("The download appears incomplete, so no incomplete file was sent.")
        return fake_result(source)

    batch_service.translator.translate.side_effect = translate
    await batch_service.process_job(JOB)
    assert batch_service.bot.send_document.await_count == 2
    summary = batch_service.bot.edit_message_text.await_args.kwargs["text"]
    assert "1 failed" in summary and "Failed: Hindi" in summary
    assert "Hindi: The download appears incomplete" in summary and "no incomplete file was sent" in summary


async def test_unexpected_source_failure_notifies_user(batch_service):
    batch_service.bot.get_file.side_effect = RuntimeError("unexpected")
    await batch_service.process_job(JOB)
    assert "Could not read the subtitle" in batch_service.bot.edit_message_text.await_args.kwargs["text"]
    batch_service.translator.translate.assert_not_awaited()


async def test_batch_session_start_failure_reports_and_cleans_up(batch_service):
    @asynccontextmanager
    async def failed_session():
        raise WebsiteError("browser unavailable")
        yield  # pragma: no cover - makes this an async context manager

    batch_service.translator.session = failed_session
    await batch_service.process_job(JOB)
    summary = batch_service.bot.edit_message_text.await_args.kwargs["text"]
    assert "Stopped" in summary and "browser session failed" in summary
    batch_service.translator.translate.assert_not_awaited()
    batch_service.bot.send_document.assert_not_awaited()
    assert not batch_service._batch_cancellations


async def test_batch_stops_immediately_after_telegram_delivery_failure(batch_service):
    batch_service.translator.translate.return_value = TranslatedSubtitle(SOURCE, 1, False)
    batch_service.bot.send_document.side_effect = TelegramError("delivery failed")
    await batch_service.process_job(JOB)
    assert batch_service.translator.translate.await_count == 1
    assert "1 failed" in batch_service.bot.edit_message_text.await_args.kwargs["text"]


async def test_cancel_command_stops_current_batch_without_sending_a_partial_file(batch_service):
    started = asyncio.Event()
    release = asyncio.Event()

    async def translate(source, code, *, on_progress=None):
        started.set()
        await release.wait()
        return fake_result(source)

    batch_service.translator.translate.side_effect = translate
    task = asyncio.create_task(batch_service.process_job(JOB))
    await asyncio.wait_for(started.wait(), 1)
    update = cancel_update()
    await batch_service.cancel_batch(update, None)
    assert "stopping" in update.effective_message.reply_text.await_args.args[0]
    await asyncio.wait_for(task, 3)
    assert batch_service.translator.translate.await_count == 1
    batch_service.bot.send_document.assert_not_awaited()
    assert "Stopped" in batch_service.bot.edit_message_text.await_args.kwargs["text"]


async def test_cancel_is_registered_before_the_start_notice_awaits(batch_service):
    started = asyncio.Event()
    release = asyncio.Event()

    async def notice(**kwargs):
        if kwargs["text"].startswith("Starting"):
            started.set()
            await release.wait()
        return SimpleNamespace(message_id=123)

    batch_service.bot.send_message.side_effect = notice
    task = asyncio.create_task(batch_service.process_job(JOB))
    await asyncio.wait_for(started.wait(), 1)
    update = cancel_update()
    await batch_service.cancel_batch(update, None)
    assert "stopping" in update.effective_message.reply_text.await_args.args[0]
    release.set()
    await asyncio.wait_for(task, 3)
    batch_service.translator.translate.assert_not_awaited()
    assert "canceled" in batch_service.bot.edit_message_text.await_args.kwargs["text"].lower()


async def test_cancel_waiting_batches_leaves_other_jobs_in_order():
    processed = []
    queue = JobQueue(lambda job: None, max_waiting=3)
    single = replace(JOB, original_message_id=78, language_code="hi", language_name="Hindi", languages=())
    other_user = replace(JOB, chat_id=99)
    assert queue.submit(JOB) is EnqueueResult.ACCEPTED
    assert queue.submit(single) is EnqueueResult.ACCEPTED
    assert queue.submit(other_user) is EnqueueResult.ACCEPTED
    joiner = asyncio.create_task(queue._queue.join())
    await asyncio.sleep(0)
    assert queue.cancel_waiting_batches(42) == 1
    await asyncio.sleep(0)
    assert not joiner.done()
    while not queue._queue.empty():
        processed.append(queue._queue.get_nowait())
        queue._queue.task_done()
    assert processed == [single, other_user]
    await asyncio.wait_for(joiner, 1)
