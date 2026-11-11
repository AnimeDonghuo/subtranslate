"""The API_ID/API_HASH path lifts the Bot API limits up to Telegram's 2 GB."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import DocumentAttributeFilename

from subtranslate.config import Settings
from subtranslate.jobs import TranslationJob
from subtranslate.large_telegram import (
    TELEGRAM_MAX_BYTES,
    LargeTelegramTransfer,
    LargeTransferError,
    ensure_telegram_size,
)

SETTINGS = Settings("12345:TEST-TOKEN", "https://example.com", api_id=123, api_hash="PRIVATE-HASH")
JOB = TranslationJob(42, 77, "file-id", "private.srt", 10, "hi", "Hindi", user_id=42)
CHANNEL = -1001234567890


def fake_client(*, document_size: int = 10, peer_error: bool = False):
    peer = object()
    message = SimpleNamespace(
        chat_id=42,
        document=SimpleNamespace(size=document_size),
        get_input_chat=AsyncMock(return_value=peer),
    )
    entities = [ValueError("not cached"), peer] if peer_error else [peer]

    async def get_input_entity(_chat_id):
        value = entities.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def iter_dialogs(limit=None):
        assert limit is None
        return
        yield  # pragma: no cover - keeps this an async generator

    client = SimpleNamespace(
        connect=AsyncMock(),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=12345, bot=True)),
        get_messages=AsyncMock(return_value=message),
        download_media=AsyncMock(),
        upload_file=AsyncMock(return_value="handle"),
        send_file=AsyncMock(),
        get_input_entity=AsyncMock(side_effect=get_input_entity),
        iter_dialogs=iter_dialogs,
        disconnect=AsyncMock(),
    )
    return client, message, peer


def test_telegram_two_gigabyte_ceiling():
    ensure_telegram_size(None)
    ensure_telegram_size(0)
    ensure_telegram_size(TELEGRAM_MAX_BYTES)
    with pytest.raises(LargeTransferError, match="2 GB"):
        ensure_telegram_size(TELEGRAM_MAX_BYTES + 1)


async def test_an_oversized_document_is_refused_before_downloading(tmp_path):
    client, _message, _peer = fake_client(document_size=TELEGRAM_MAX_BYTES + 1)
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    with pytest.raises(LargeTransferError, match="2 GB"):
        await transfer.download(JOB, tmp_path / "source.srt")
    client.download_media.assert_not_awaited()


async def test_log_channel_copy_uses_mtproto_and_html(tmp_path):
    client, _message, peer = fake_client()
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    path = tmp_path / "movie.Hindi.srt"
    path.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nNamaste\n\n")

    await transfer.send_file_to(CHANNEL, path, "movie.Hindi.srt", "<b>caption</b>")

    client.get_input_entity.assert_awaited_once_with(CHANNEL)
    client.upload_file.assert_awaited_once_with(str(path), part_size_kb=512, file_name="movie.Hindi.srt")
    send = client.send_file.await_args
    assert send.args == (peer,)
    assert send.kwargs["force_document"] is True
    assert send.kwargs["parse_mode"] == "html"
    assert send.kwargs["caption"] == "<b>caption</b>"
    assert send.kwargs["attributes"] == [DocumentAttributeFilename("movie.Hindi.srt")]


async def test_log_channel_entity_is_refreshed_once_when_not_cached(tmp_path):
    client, _message, peer = fake_client(peer_error=True)
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    path = tmp_path / "movie.Hindi.srt"
    path.write_bytes(b"x")

    await transfer.send_file_to(CHANNEL, path, "movie.Hindi.srt", "caption")

    assert client.get_input_entity.await_count == 2
    assert client.send_file.await_args.args == (peer,)
    # The resolved peer is cached for the next delivery.
    await transfer.send_file_to(CHANNEL, path, "movie.Hindi.srt", "caption")
    assert client.get_input_entity.await_count == 2


async def test_missing_credentials_still_block_the_large_copy(tmp_path):
    transfer = LargeTelegramTransfer(Settings("12345:TEST-TOKEN", "https://example.com"))
    path = tmp_path / "movie.Hindi.srt"
    path.write_bytes(b"x")
    with pytest.raises(LargeTransferError, match="API_ID and API_HASH"):
        await transfer.send_file_to(CHANNEL, path, "movie.Hindi.srt", "caption")


async def test_upload_failure_is_reported_without_leaking_details(tmp_path):
    client, _message, _peer = fake_client()
    client.send_file = AsyncMock(side_effect=RuntimeError("secret-channel-detail"))
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    path = tmp_path / "private.srt"
    path.write_bytes(b"x")
    with pytest.raises(LargeTransferError) as error:
        await transfer.send_file_to(CHANNEL, Path(path), "private.srt", "caption")
    assert "secret-channel-detail" not in str(error.value)
