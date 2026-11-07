"""MTProto integration is mocked; no Telegram account or credentials are used."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import DocumentAttributeFilename

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.jobs import TranslationJob
from subtranslate.languages import LanguageCatalog
from subtranslate.large_srt import FileSubtitle
from subtranslate.large_telegram import (
    BOT_API_DOWNLOAD_BYTES,
    BOT_API_UPLOAD_BYTES,
    LargeTelegramTransfer,
    LargeTransferError,
)
from subtranslate.subtitles import TranslatedSubtitle

SOURCE = b"1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n"
JOB = TranslationJob(42, 77, "telegram-file-id", "private.srt", len(SOURCE), "hi", "Hindi")
SETTINGS = Settings("12345:TEST-TOKEN", "https://example.com", api_id=123, api_hash="PRIVATE-HASH")


def fake_client(*, chat_id=42, bot_id=12345):
    peer = object()
    message = SimpleNamespace(chat_id=chat_id, document=SimpleNamespace(size=len(SOURCE)),
                              get_input_chat=AsyncMock(return_value=peer))

    async def download_media(received, *, file):
        assert received is message
        Path(file).write_bytes(SOURCE)
        return file

    client = SimpleNamespace(
        connect=AsyncMock(), sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=bot_id, bot=True)),
        get_messages=AsyncMock(return_value=message), download_media=AsyncMock(side_effect=download_media),
        upload_file=AsyncMock(return_value=object()), send_file=AsyncMock(), disconnect=AsyncMock(),
    )
    return client, message, peer


async def test_mtproto_uses_bot_identity_fresh_message_and_private_peer(tmp_path):
    client, message, peer = fake_client()
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    path = tmp_path / "source.srt"
    await transfer.download(JOB, path)
    await transfer.send(JOB, path, "private.Hindi.srt", "caption")
    assert path.read_bytes() == SOURCE
    client.sign_in.assert_awaited_once_with(bot_token=SETTINGS.bot_token)
    client.get_messages.assert_awaited_once_with(None, ids=77)
    client.download_media.assert_awaited_once_with(message, file=str(path))
    client.upload_file.assert_awaited_once_with(str(path), part_size_kb=512, file_name="private.Hindi.srt")
    send = client.send_file.await_args
    assert send.args == (peer,) and send.kwargs["file"] is client.upload_file.return_value
    assert send.kwargs["reply_to"] == 77 and send.kwargs["force_document"]
    assert send.kwargs["parse_mode"] is None
    assert send.kwargs["attributes"] == [DocumentAttributeFilename("private.Hindi.srt")]
    await transfer.close()
    client.disconnect.assert_awaited_once()


async def test_missing_credentials_only_block_large_transfer():
    transfer = LargeTelegramTransfer(Settings("12345:TEST-TOKEN", "https://example.com"))
    with pytest.raises(LargeTransferError, match="API_ID and API_HASH"):
        await transfer.start()


async def test_telethon_internal_logs_cannot_expose_request_secrets(monkeypatch, caplog):
    import logging

    from subtranslate import large_telegram

    client, _, _ = fake_client()
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(large_telegram, "TelegramClient", factory)
    transfer = LargeTelegramTransfer(SETTINGS)
    await transfer.start()
    internal = captured["base_logger"].getChild("telethon.network.mtprotosender")
    internal.error("SECRET TOKEN AND HASH in raw request")
    assert internal.getEffectiveLevel() > logging.CRITICAL
    assert "SECRET TOKEN" not in caplog.text
    await transfer.close()


async def test_wrong_bot_identity_and_private_data_are_not_logged(caplog):
    client, _, _ = fake_client(bot_id=999)
    client.sign_in.side_effect = RuntimeError("PRIVATE-HASH 12345:TEST-TOKEN")
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    with pytest.raises(LargeTransferError, match="Could not connect") as exc:
        await transfer.start()
    assert "PRIVATE-HASH" not in str(exc.value) and "TEST-TOKEN" not in str(exc.value)
    assert "PRIVATE-HASH" not in caplog.text and "TEST-TOKEN" not in caplog.text
    client.disconnect.assert_awaited_once()


async def test_rejects_original_message_from_another_chat(tmp_path):
    client, _, _ = fake_client(chat_id=88)
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    with pytest.raises(LargeTransferError, match="original Telegram document"):
        await transfer.download(JOB, tmp_path / "source.srt")
    client.download_media.assert_not_awaited()
    await transfer.close()


async def test_rejects_incomplete_mtproto_download(tmp_path):
    client, _, _ = fake_client()
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    with pytest.raises(LargeTransferError, match="incomplete"):
        await transfer.download(replace(JOB, file_size=len(SOURCE) + 1), tmp_path / "source.srt")
    client.send_file.assert_not_awaited()
    await transfer.close()


async def test_mtproto_checks_original_document_size_when_bot_update_has_no_size(tmp_path):
    client, message, _ = fake_client()
    message.document.size += 1
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    with pytest.raises(LargeTransferError, match="incomplete"):
        await transfer.download(replace(JOB, file_size=None), tmp_path / "source.srt")
    await transfer.close()


async def test_large_source_uses_mtproto_not_public_bot_api(tmp_path):
    service = BotService(SETTINGS, LanguageCatalog(), SimpleNamespace())
    service.bot = SimpleNamespace(get_file=AsyncMock())

    async def download(job, path):
        path.write_bytes(SOURCE)

    transport = SimpleNamespace(download=AsyncMock(side_effect=download))
    source = await service._download_source(replace(JOB, file_size=BOT_API_DOWNLOAD_BYTES + 1), tmp_path, transport)
    assert isinstance(source, FileSubtitle) and source.cue_count == 1
    transport.download.assert_awaited_once()
    service.bot.get_file.assert_not_awaited()


async def test_large_result_uses_mtproto_and_keeps_single_download_on_disk(tmp_path):
    service = BotService(SETTINGS, LanguageCatalog(), SimpleNamespace())
    service.bot = SimpleNamespace(send_document=AsyncMock())
    transport = SimpleNamespace(send=AsyncMock())
    path = tmp_path / "download.srt"
    with path.open("wb") as result:
        result.truncate(BOT_API_UPLOAD_BYTES + 1)  # sparse: no large fixture on disk
    await service._send_result(JOB, "Hindi", TranslatedSubtitle(path, 1, False, changed_cues=1), transport)
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[:3] == (JOB, path, "private.Hindi.srt")
    service.bot.send_document.assert_not_awaited()


async def test_canceling_mtproto_download_propagates_and_closes_session(tmp_path):
    client, _, _ = fake_client()
    started = asyncio.Event()

    async def slow_download(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    client.download_media.side_effect = slow_download
    transfer = LargeTelegramTransfer(SETTINGS, client=client)
    task = asyncio.create_task(transfer.download(JOB, tmp_path / "source.srt"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transfer.close()
    client.disconnect.assert_awaited_once()
