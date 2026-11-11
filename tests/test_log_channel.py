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

    assert [item["chat_id"] for item in service.bot.documents] == [42, CHANNEL]
    copy = service.bot.documents[-1]
    assert copy["document"].filename == "movie.Hindi.srt"
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

    assert [item["chat_id"] for item in service.bot.documents] == [42, CHANNEL]
    assert service.bot.documents[0]["document"].filename == "movie.Hindi.srt"


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
    path = tmp_path / "movie.Hindi.srt"
    path.write_bytes(RESULT.content)
    result = TranslatedSubtitle(content=path, cue_count=1, unchanged=False, changed_cues=1)
    transport = FakeTransport()

    await service._send_result(JOB, "Hindi", result, transport)

    assert transport.sent == [(42, "movie.Hindi.srt")]
    assert [copy[0] for copy in transport.copies] == [CHANNEL]
    assert "Asha Kumari" in transport.copies[0][2] and "@asha" in transport.copies[0][2]
    assert service.bot.documents == []


async def test_failed_large_channel_copy_still_records_and_delivers(tmp_path, monkeypatch):
    import subtranslate.bot as bot_module

    monkeypatch.setattr(bot_module, "BOT_API_UPLOAD_BYTES", 4)
    service = make_service(owner=1, log_channel=CHANNEL)
    service.bot = FakeBot()
    path = tmp_path / "movie.Hindi.srt"
    path.write_bytes(RESULT.content)
    result = TranslatedSubtitle(content=path, cue_count=1, unchanged=False, changed_cues=1)
    transport = FakeTransport(fail_copy=True)

    await service._send_result(JOB, "Hindi", result, transport)

    assert transport.sent == [(42, "movie.Hindi.srt")]
    notice = service.bot.messages[-1]
    assert notice["chat_id"] == CHANNEL
    assert "could not be sent" in notice["text"]
    assert (await service.store.stats()).translations_total == 1
