"""A flaky MTProto part must not throw away a whole large download."""

import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError

from subtranslate.config import Settings
from subtranslate.jobs import TranslationJob
from subtranslate.large_telegram import (
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_RETRIES,
    LargeTelegramTransfer,
    LargeTransferError,
    missing_ranges,
)

SETTINGS = Settings("12345:TOKEN", "https://example.com", api_id=123, api_hash="HASH")
PAYLOAD = bytes(range(256)) * 12_000                     # ~3 MB, several parts each
JOB = TranslationJob(42, 77, "file-id", "bcs.ass", len(PAYLOAD), "hi", "Hindi")


def make_client(payload: bytes, *, iter_download=None):
    message = SimpleNamespace(chat_id=42, document=SimpleNamespace(size=len(payload)),
                              get_input_chat=AsyncMock(return_value=object()))

    async def download_media(received, *, file):
        Path(file).write_bytes(payload)
        return file

    client = SimpleNamespace(
        connect=AsyncMock(), sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=12345, bot=True)),
        get_messages=AsyncMock(return_value=message),
        download_media=AsyncMock(side_effect=download_media),
        upload_file=AsyncMock(return_value=object()), send_file=AsyncMock(), disconnect=AsyncMock(),
    )
    if iter_download is not None:
        client.iter_download = iter_download
    return client, message


def striding_client(payload: bytes, *, fail_first: int = 0, pieces: int | None = None):
    """Serve offset/stride parts, optionally failing or stopping early."""
    calls = {"failures": 0, "offsets": []}

    async def iter_download(message, *, offset=0, stride=None, request_size=DOWNLOAD_CHUNK_BYTES,
                            file_size=None):
        step = stride or request_size
        calls["offsets"].append(offset)
        if stride is not None and calls["failures"] < fail_first and offset != 0:
            calls["failures"] += 1
            raise ConnectionResetError("part lost")
        position = offset
        served = 0
        while position < len(payload) and (pieces is None or served < pieces):
            yield payload[position:position + request_size]
            served += 1
            position += step

    return iter_download, calls


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Retries are about recovery, not about sleeping in the test suite."""

    async def instant(_self, _delay):
        return None

    monkeypatch.setattr(LargeTelegramTransfer, "_pause", instant)


async def download(client, job=JOB, name="source.ass"):
    path = Path(tmp) / name
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    await transfer.download(job, path)
    await transfer.close()
    return path


tmp = ""


@pytest.fixture(autouse=True)
def _tmpdir(tmp_path):
    global tmp
    tmp = str(tmp_path)
    yield
    tmp = ""


async def test_a_lost_part_is_resumed_instead_of_failing_the_download():
    iter_download, calls = striding_client(PAYLOAD, fail_first=1)
    client, _message = make_client(PAYLOAD, iter_download=iter_download)

    path = await download(client)

    assert path.read_bytes() == PAYLOAD
    assert calls["failures"] == 1
    client.download_media.assert_not_awaited()


async def test_a_flood_wait_is_waited_out_and_the_part_is_resumed():
    calls = {"waits": [], "failed": False}

    async def iter_download(message, *, offset=0, stride=None, request_size=DOWNLOAD_CHUNK_BYTES,
                            file_size=None):
        step = stride or request_size
        if stride is not None and offset == DOWNLOAD_CHUNK_BYTES and not calls["failed"]:
            calls["failed"] = True
            raise FloodWaitError(request=None, capture=3)
        position = offset
        while position < len(PAYLOAD):
            yield PAYLOAD[position:position + request_size]
            position += step

    client, _message = make_client(PAYLOAD, iter_download=iter_download)
    transfer = LargeTelegramTransfer(SETTINGS, client=client)

    async def record(delay):
        calls["waits"].append(delay)

    transfer._pause = record
    path = Path(tmp) / "source.ass"
    await transfer.download(JOB, path)

    assert path.read_bytes() == PAYLOAD
    assert calls["waits"] == [3.0]


async def test_parts_left_unwritten_are_refetched_one_at_a_time():
    iter_download, _calls = striding_client(PAYLOAD, pieces=1)   # every stream stops early
    client, _message = make_client(PAYLOAD, iter_download=iter_download)

    path = await download(client)

    assert path.read_bytes() == PAYLOAD


async def test_a_broken_parallel_download_falls_back_to_one_stream():
    async def iter_download(*args, **kwargs):
        raise ConnectionResetError("cannot stream parts")
        yield b""                                                # pragma: no cover

    client, _message = make_client(PAYLOAD, iter_download=iter_download)

    path = await download(client)

    assert path.read_bytes() == PAYLOAD
    client.download_media.assert_awaited_once()


async def test_the_failure_message_names_the_errors_and_stays_safe():
    async def iter_download(*args, **kwargs):
        raise ConnectionResetError("secret file reference")
        yield b""                                                # pragma: no cover

    client, _message = make_client(PAYLOAD, iter_download=iter_download)
    client.download_media.side_effect = TimeoutError("PRIVATE-HASH")
    transfer = LargeTelegramTransfer(SETTINGS, client=client)

    with pytest.raises(LargeTransferError) as raised:
        await transfer.download(JOB, Path(tmp) / "source.ass")

    text = str(raised.value)
    assert "ConnectionResetError" in text and "TimeoutError" in text
    assert "PRIVATE-HASH" not in text and "secret file reference" not in text


async def test_a_connection_error_is_not_reported_as_a_disk_problem():
    async def iter_download(*args, **kwargs):
        raise ConnectionResetError("reset")
        yield b""                                                # pragma: no cover

    client, _message = make_client(PAYLOAD, iter_download=iter_download)
    client.download_media.side_effect = ConnectionResetError("reset")
    transfer = LargeTelegramTransfer(SETTINGS, client=client)

    with pytest.raises(LargeTransferError) as raised:
        await transfer.download(JOB, Path(tmp) / "source.ass")

    assert "disk" not in str(raised.value)
    assert "ConnectionResetError" in str(raised.value)


async def test_running_out_of_disk_says_so():
    async def iter_download(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
        yield b""                                                # pragma: no cover

    client, _message = make_client(PAYLOAD, iter_download=iter_download)
    client.download_media.side_effect = OSError(errno.ENOSPC, "No space left on device")
    transfer = LargeTelegramTransfer(SETTINGS, client=client)

    with pytest.raises(LargeTransferError, match="disk space"):
        await transfer.download(JOB, Path(tmp) / "source.ass")


def test_retry_policy_honours_flood_waits_and_gives_up_eventually():
    transfer = LargeTelegramTransfer(SETTINGS, client=object())

    assert transfer._retry_delay(FloodWaitError(request=None, capture=7), 1) == 7.0
    assert transfer._retry_delay(ConnectionResetError(), 1) == 2.0
    assert transfer._retry_delay(ValueError(), DOWNLOAD_RETRIES + 1) is None


def test_missing_ranges_covers_gaps_and_tails():
    assert missing_ranges([(0, 10), (10, 20)], 20) == []
    assert missing_ranges([(0, 10), (15, 20)], 20) == [(10, 15)]
    assert missing_ranges([(5, 10)], 20) == [(0, 5), (10, 20)]
    assert missing_ranges([], 20) == [(0, 20)]
    assert missing_ranges([(0, 5)], 5) == []


async def test_a_size_mismatch_is_reported_and_nothing_is_sent():
    client, message = make_client(PAYLOAD)
    message.document.size = len(PAYLOAD) + 1
    transfer = LargeTelegramTransfer(SETTINGS, client=client)

    with pytest.raises(LargeTransferError, match="incomplete"):
        await transfer.download(replace(JOB, file_size=None), Path(tmp) / "source.ass")

    transfer_never_sent = client.send_file.await_count == 0
    assert transfer_never_sent
