"""Large non-SRT files go through MTProto, and their ceiling follows free RAM."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from subtranslate import bot as bot_module
from subtranslate.bot import BotService, _check_in_memory_size
from subtranslate.config import Settings
from subtranslate.jobs import TranslationJob
from subtranslate.languages import LanguageCatalog
from subtranslate.subtitles import SubtitleError

VTT = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello\n\n"


@pytest.fixture
def service():
    return BotService(Settings("12345:TESTTOKEN", "https://example.com"),
                      LanguageCatalog(), SimpleNamespace())


class FakeTransport:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.downloads = []
        self.closed = False

    async def download(self, job, path, **kwargs):
        self.downloads.append(path)
        path.write_bytes(self.payload)
        return path

    async def close(self):
        self.closed = True


async def test_large_vtt_uses_mtproto_instead_of_the_bot_api(service, tmp_path):
    transport = FakeTransport(VTT.encode())
    service.bot = SimpleNamespace(get_file=AsyncMock())
    job = TranslationJob(42, 77, "file", "movie.vtt", 30_000_000, "hi", "Hindi")

    source = await service._download_source(job, tmp_path, transport)

    assert transport.downloads == [tmp_path / "source.srt"]
    assert source.extension == ".vtt" and len(source.cues) == 1
    service.bot.get_file.assert_not_awaited()


async def test_huge_non_srt_file_reports_free_memory_instead_of_a_fixed_cap(
        service, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "available_memory_mb", lambda: 300)
    job = TranslationJob(42, 77, "file", "movie.sub", 400_000_000, "hi", "Hindi")

    with pytest.raises(SubtitleError) as error:
        await service._download_source(job, tmp_path, FakeTransport(b""))

    message = str(error.value)
    assert "400 MB" in message and "100 MB" in message
    assert "SRT" in message


def test_srt_is_never_limited_by_free_memory(monkeypatch):
    monkeypatch.setattr(bot_module, "available_memory_mb", lambda: 300)
    _check_in_memory_size(1_900_000_000, ".srt")


def test_unknown_memory_or_size_does_not_block_the_file(monkeypatch):
    monkeypatch.setattr(bot_module, "available_memory_mb", lambda: None)
    _check_in_memory_size(1_900_000_000, ".vtt")
    monkeypatch.setattr(bot_module, "available_memory_mb", lambda: 300)
    _check_in_memory_size(None, ".vtt")
