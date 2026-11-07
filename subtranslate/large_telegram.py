"""Optional MTProto transfers for files beyond the public Bot API limits.

The webhook, buttons and status messages still use the Bot API. MTProto is
only opened on demand, authenticates as the *same bot*, and keeps its session
in memory. Fetching the original private-chat message supplies a fresh file
reference and the peer's access hash; Bot API file IDs alone do not reliably
contain those values.
"""

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import DocumentAttributeFilename

from .config import Settings
from .jobs import TranslationJob

logger = logging.getLogger(__name__)
# Telethon may include complete request objects in error logs, including bot
# tokens, API hashes, captions or filenames. Keep its internal logger silent;
# this module logs only sanitized exception class names instead.
_transport_logger = logging.getLogger("subtranslate.mtproto_internal")
_transport_logger.setLevel(logging.CRITICAL + 1)
_transport_logger.propagate = False

# Limits of the public, remotely hosted Bot API, not limits for an SRT job.
BOT_API_DOWNLOAD_BYTES = 20_000_000
BOT_API_UPLOAD_BYTES = 50_000_000


class LargeTransferError(Exception):
    """A safe, user-facing transfer failure; never include secrets or paths."""


class LargeTelegramTransfer:
    def __init__(self, settings: Settings, *, client=None) -> None:
        self.settings = settings
        self.client = client
        self._started = False
        self._message = None
        self._message_key = None

    async def start(self) -> None:
        if self._started:
            return
        if not self.settings.api_id or not self.settings.api_hash:
            raise LargeTransferError(
                "Large Telegram files require both API_ID and API_HASH in the deployment settings. "
                "The ordinary Bot API cannot download files over 20 MB or upload files over 50 MB."
            )
        if self.client is None:
            self.client = TelegramClient(
                MemorySession(), self.settings.api_id, self.settings.api_hash,
                receive_updates=False, flood_sleep_threshold=0, request_retries=2,
                connection_retries=3, retry_delay=1, base_logger=_transport_logger,
            )
        try:
            async with asyncio.timeout(90):
                await self.client.connect()
                await self.client.sign_in(bot_token=self.settings.bot_token)
                me = await self.client.get_me()
                expected_id = int(self.settings.bot_token.split(":", 1)[0])
                if not me or not me.bot or me.id != expected_id:
                    raise ValueError("MTProto bot identity mismatch")
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:  # noqa: BLE001 - Telegram errors can contain credentials
            logger.warning("MTProto connection failed (%s)", type(exc).__name__)
            await self.close()
            raise LargeTransferError(
                "Could not connect the large-file Telegram transport. Check the deployment's "
                "API_ID, API_HASH, bot token and network access, then try again."
            ) from None
        self._started = True

    async def _original(self, job: TranslationJob):
        await self.start()
        key = (job.chat_id, job.original_message_id)
        if self._message_key == key:
            return self._message
        try:
            # messages.getMessages by ID works for private chats without
            # already having a cached access hash. Do not use this for channels.
            message = await self.client.get_messages(None, ids=job.original_message_id)
            if message is None or message.chat_id != job.chat_id or message.document is None:
                raise ValueError("original private-chat document unavailable")
            # Resolve while the returned message still has its user entities.
            await message.get_input_chat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - may contain private message data
            logger.warning("MTProto original document lookup failed (%s)", type(exc).__name__)
            raise LargeTransferError(
                "Could not access the original Telegram document for a large transfer. "
                "Please send the file again in a private chat and retry."
            ) from None
        self._message = message
        self._message_key = key
        return message

    async def download(self, job: TranslationJob, path: Path) -> None:
        message = await self._original(job)
        try:
            saved = await self.client.download_media(message, file=str(path))
            if not saved or not path.is_file():
                raise OSError("download did not create the destination")
            actual = path.stat().st_size
            document_size = message.document.size
            if (actual <= 0 or actual != document_size
                    or (job.file_size is not None and actual != job.file_size)):
                raise ValueError("incomplete Telegram download")
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.warning("Large Telegram download failed locally (%s)", type(exc).__name__)
            raise LargeTransferError(
                "Could not save the Telegram file. The deployment may not have enough temporary disk space; "
                "no partial subtitle was sent."
            ) from None
        except Exception as exc:  # noqa: BLE001 - may include private data
            logger.warning("Large Telegram download failed (%s)", type(exc).__name__)
            raise LargeTransferError(
                "The Telegram file download failed or was incomplete. Please send the file again and retry."
            ) from None

    async def send(self, job: TranslationJob, path: Path, filename: str, caption: str) -> None:
        message = await self._original(job)
        try:
            peer = await message.get_input_chat()
            if peer is None:
                raise ValueError("private-chat peer unavailable")
            # send_file's **kwargs do not forward a part_size_kb to Telethon's
            # uploader. Upload explicitly so large files are read in 512 KiB
            # chunks instead of buffering the complete file.
            handle = await self.client.upload_file(str(path), part_size_kb=512, file_name=filename)
            await self.client.send_file(
                peer, file=handle, force_document=True, reply_to=job.original_message_id,
                attributes=[DocumentAttributeFilename(filename)], caption=caption, parse_mode=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - may include request contents
            logger.warning("Large Telegram upload failed (%s)", type(exc).__name__)
            raise LargeTransferError(
                "Telegram could not send the complete translated file. It may exceed Telegram's bot upload "
                "limit or the connection may have failed; please try again."
            ) from None

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning("MTProto cleanup failed (%s)", type(exc).__name__)
        self._started = False
        self._message = None
        self._message_key = None
