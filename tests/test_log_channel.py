"""Every delivered subtitle is counted and copied to the owner's log channel."""

from pathlib import Path

from support import FakeBot, make_service

from subtranslate.jobs import TranslationJob
from subtranslate.subtitles import TranslatedSubtitle

CHANNEL = -1001234567890
RESULT = TranslatedSubtitle(
    content=b"1\n00:00:01,000 --> 00:00:02,000\nNamaste\n\n",
    cue_count=1, unchanged=False, changed_cues=1,
)
JOB = TranslationJob(
    chat_id=42, original_message_id=77, file_id="file1", file_name="movie.srt",
    file_size=10, language_code="hi", language_name="Hindi",
    user_id=42, user_name="Asha Kumari", username="asha",
)


class NoTransport:
    async def send(self, *args, **kwargs):  # pragma: no cover - must not be used here
        raise AssertionError("MTProto must not be used for a small result")


async def test_delivery_is_recorded_and_copied_to_the_log_channel():
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()

    await service._send_result(JOB, "Hindi", RESULT, NoTransport())

    assert [item["chat_id"] for item in service.bot.documents] == [42, CHANNEL, CHANNEL]
    copy = service.bot.documents[1]
    assert copy["document"].filename == "Hindi.srt"
    assert "Asha Kumari" in copy["caption"]
    assert "@asha" in copy["caption"]
    assert "<code>42</code>" in copy["caption"]
    assert "Hindi" in copy["caption"]
    assert "movie.srt" in copy["caption"]
    assert copy.get("parse_mode") == "HTML"

    stats = await service.store.stats()
    assert stats.translations_total == 1
    assert stats.translations_day == 1


async def test_username_is_reported_as_missing_when_absent():
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    job = TranslationJob(
        chat_id=7, original_message_id=8, file_id="f", file_name="clip.srt",
        file_size=10, language_code="es", language_name="Spanish",
        user_id=7, user_name="No Username", username="",
    )
    await service._send_result(job, "Spanish", RESULT, NoTransport())
    assert "not set" in service.bot.documents[-1]["caption"]


async def test_a_broken_log_channel_never_blocks_delivery(caplog):
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    service.bot.fail_chat_ids = {CHANNEL}

    await service._send_result(JOB, "Hindi", RESULT, NoTransport())

    assert [item["chat_id"] for item in service.bot.documents] == [42]
    assert "Log channel" in caplog.text
    # The audit record is still stored even if the channel refuses the copy.
    assert (await service.store.stats()).translations_total == 1


async def test_no_channel_configured_sends_only_to_the_user():
    service = make_service(owner=1)
    service.bot = FakeBot()
    await service._send_result(JOB, "Hindi", RESULT, NoTransport())
    assert [item["chat_id"] for item in service.bot.documents] == [42]
    assert (await service.store.stats()).translations_total == 1


async def test_large_result_on_disk_is_copied_from_the_file(tmp_path: Path):
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    path = tmp_path / "download.srt"
    path.write_bytes(RESULT.content)
    result = TranslatedSubtitle(content=path, cue_count=1, unchanged=False, changed_cues=1)

    await service._send_result(JOB, "Hindi", result, NoTransport())

    assert [item["chat_id"] for item in service.bot.documents] == [42, CHANNEL, CHANNEL]
    assert service.bot.documents[0]["document"].filename == "Hindi.srt"
    # The second channel message is the user's original, by file id.
    assert service.bot.documents[2]["document"] == "file1"


class FakeTransport:
    def __init__(self, *, fail_copy=False):
        self.sent = []
        self.copies = []
        self.fail_copy = fail_copy

    async def send(self, job, path, filename, caption):
        self.sent.append((job.chat_id, filename))

    async def send_file_to(self, chat_id, path, filename, caption):
        if self.fail_copy:
            from subtranslate.large_telegram import LargeTransferError

            raise LargeTransferError("channel refused")
        self.copies.append((chat_id, filename, caption))


async def test_large_results_use_mtproto_for_the_user_and_the_channel(tmp_path, monkeypatch):
    import subtranslate.bot as bot_module

    # Force the >Bot-API branch with a tiny threshold instead of a huge file.
    monkeypatch.setattr(bot_module, "BOT_API_UPLOAD_BYTES", 4)
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    path = tmp_path / "Hindi.srt"
    path.write_bytes(RESULT.content)
    result = TranslatedSubtitle(content=path, cue_count=1, unchanged=False, changed_cues=1)
    transport = FakeTransport()

    await service._send_result(JOB, "Hindi", result, transport)

    assert transport.sent == [(42, "Hindi.srt")]
    assert [copy[0] for copy in transport.copies] == [CHANNEL]
    assert "Asha Kumari" in transport.copies[0][2] and "@asha" in transport.copies[0][2]
    # The original is copied too; without a local copy it is resent by file id.
    assert [item["chat_id"] for item in service.bot.documents] == [CHANNEL]
    assert service.bot.documents[0]["document"] == "file1"


async def test_failed_large_channel_copy_still_records_and_delivers(tmp_path, monkeypatch):
    import subtranslate.bot as bot_module

    monkeypatch.setattr(bot_module, "BOT_API_UPLOAD_BYTES", 4)
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    path = tmp_path / "Hindi.srt"
    path.write_bytes(RESULT.content)
    result = TranslatedSubtitle(content=path, cue_count=1, unchanged=False, changed_cues=1)
    transport = FakeTransport(fail_copy=True)

    await service._send_result(JOB, "Hindi", result, transport)

    assert transport.sent == [(42, "Hindi.srt")]
    notice = service.bot.messages[-1]
    assert notice["chat_id"] == CHANNEL
    assert "could not be sent" in notice["text"]
    assert (await service.store.stats()).translations_total == 1


async def test_the_original_file_is_copied_with_its_own_name(tmp_path: Path):
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    source = tmp_path / "source.srt"
    source.write_bytes(b"10\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
    job = TranslationJob(
        chat_id=42, original_message_id=77, file_id="file1",
        file_name=r"C:\videos\movie name.srt", file_size=41,
        language_code="hi", language_name="Hindi",
        user_id=42, user_name="Asha Kumari", username="asha",
    )

    await service._log_channel_source(job, "Hindi", source, None)

    copy = service.bot.documents[-1]
    assert copy["chat_id"] == CHANNEL
    assert copy["document"].filename == "movie name.srt"
    assert "Original subtitle" in copy["caption"]
    assert "movie name.srt" in copy["caption"] and "Hindi" in copy["caption"]
    assert copy["parse_mode"] == "HTML"


async def test_the_original_is_copied_only_once_per_job(tmp_path: Path):
    # A batch runs several languages over one source file: one copy is enough.
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()

    await service._log_channel_source(JOB, "Hindi", None, None)
    await service._log_channel_source(JOB, "Spanish", None, None)

    assert len(service.bot.documents) == 1


async def test_the_source_copy_does_not_affect_the_translation_or_its_record(tmp_path: Path):
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    service.bot.fail_chat_ids.add(CHANNEL)   # the channel refuses everything

    await service._log_channel_source(JOB, "Hindi", None, None)

    assert (await service.store.stats()).translations_total == 0
    assert service.bot.documents == []


async def test_a_large_source_is_copied_over_mtproto(tmp_path: Path, monkeypatch):
    import subtranslate.bot as bot_module

    monkeypatch.setattr(bot_module, "BOT_API_UPLOAD_BYTES", 4)
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    source = tmp_path / "source.ass"
    source.write_bytes(b"x" * 100)
    transport = FakeTransport()

    await service._log_channel_source(JOB, "Hindi", source, transport)

    assert transport.copies[0][0] == CHANNEL
    assert transport.copies[0][1] == "movie.srt"
    assert "Original subtitle" in transport.copies[0][2]


def test_the_source_path_is_found_for_disk_and_memory_sources(tmp_path: Path):
    from types import SimpleNamespace

    from subtranslate.bot import BotService

    on_disk = tmp_path / "downloaded.srt"
    on_disk.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHi\n\n")
    # SRT jobs keep the download in a FileSubtitle with a path.
    assert BotService._job_source_path(SimpleNamespace(path=on_disk), tmp_path) == on_disk

    # Formats read into memory keep the MTProto download at source.srt.
    directory = tmp_path / "job"
    directory.mkdir()
    (directory / "source.srt").write_bytes(b"x")
    assert BotService._job_source_path(object(), directory) == directory / "source.srt"
    assert BotService._job_source_path(object(), tmp_path / "empty") is None


async def test_a_failed_job_forwards_the_reason_and_the_source_file(tmp_path: Path):
    from subtranslate.subtitles import SubtitleError

    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    source = tmp_path / "original.srt"
    source.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")

    await service._report_failure(JOB, SubtitleError("The subtitle has no readable cues."),
                                  source_path=source, stage="reading the subtitle")

    copy = service.bot.documents[-1]
    assert copy["chat_id"] == CHANNEL
    assert copy["document"].filename == "movie.srt"           # the source's own name
    from telegram import InputFile

    assert isinstance(copy["document"], InputFile)   # the local download, not a file id
    assert "Translation failed" in copy["caption"]
    assert "no readable cues" in copy["caption"]
    assert "Asha Kumari" in copy["caption"] and "@asha" in copy["caption"]
    assert "reading the subtitle" in copy["caption"] and "SubtitleError" in copy["caption"]


async def test_a_failure_without_a_local_file_is_resent_by_file_id():
    from subtranslate.subtitles import SubtitleError

    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()

    await service._report_failure(JOB, SubtitleError("The website timed out."))

    copy = service.bot.documents[-1]
    assert copy["document"] == "file1"
    assert "movie.srt" in copy["caption"]


async def test_an_unexpected_error_is_named_by_type_without_its_message():
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()

    await service._report_failure(JOB, RuntimeError("SECRET-TOKEN and a private URL"))

    caption = service.bot.documents[-1]["caption"]
    assert "RuntimeError" in caption
    assert "SECRET-TOKEN" not in caption and "private URL" not in caption


async def test_a_failure_report_never_breaks_a_job(caplog):
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    service.bot.fail_chat_ids.add(CHANNEL)

    await service._report_failure(JOB, "The website returned an error.")

    # Best effort: the failure was swallowed, and the job is unaffected.
    assert service.bot.documents == []


async def test_failure_reporting_is_skipped_when_no_channel_is_configured():
    service = make_service(owner=1)
    service.bot = FakeBot()

    await service._report_failure(JOB, "anything")

    assert service.bot.documents == [] and service.bot.messages == []


async def test_the_delivered_copy_flags_text_that_did_not_change():
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    partial = TranslatedSubtitle(content=RESULT.content, cue_count=100, unchanged=False,
                                 changed_cues=60, longest_unchanged_run=40,
                                 longest_run_start=60, unchanged_cues=40)

    await service._log_channel_copy(JOB, "Hindi", "Hindi.srt", partial, len(RESULT.content))

    caption = service.bot.documents[0]["caption"]
    assert "Quality" in caption and "40 cues in a row" in caption
