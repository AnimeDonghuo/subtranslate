"""Optional MTProto transfers for files beyond the public Bot API limits.

The webhook, buttons and status messages still use the Bot API. MTProto is
only opened on demand, authenticates as the *same bot*, and keeps its session
in memory. Fetching the original private-chat message supplies a fresh file
reference and the peer's access hash; Bot API file IDs alone do not reliably
contain those values.
"""

import asyncio
import errno
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
# Telegram's own bot limit over MTProto, which API_ID/API_HASH unlock.
TELEGRAM_MAX_BYTES = 2_000_000_000
# MTProto parts are fetched over several connections at once. A single
# sequential stream is the slowest part of a large download; every subtitle
# format (not only SRT) uses these settings.
DOWNLOAD_CHUNK_BYTES = 512 * 1024
DOWNLOAD_WORKERS = 4
# Below this size one request is already fast enough to be worth no extra
# connections (and Telegram charges per-request overhead).
PARALLEL_DOWNLOAD_MIN_BYTES = 2 * 1024 * 1024
# A free instance drops connections mid-download; one lost part must not throw
# away the whole transfer. Each stream resumes from its own position.
DOWNLOAD_RETRIES = 4
DOWNLOAD_MAX_BACKOFF = 15.0
# Telegram may ask a download to slow down; honour it instead of failing the job.
DOWNLOAD_MAX_WAIT = 120.0
# Errors that really mean "no space left", not "the connection broke".
_DISK_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG, errno.EROFS})


class LargeTransferError(Exception):
    """A safe, user-facing transfer failure; never include secrets or paths."""


def ensure_telegram_size(size: int | None) -> None:
    """Refuse a file Telegram itself cannot transfer, before any upload."""
    if size is not None and size > TELEGRAM_MAX_BYTES:
        raise LargeTransferError(
            "Telegram bots can transfer at most about 2 GB per file. "
            "Split the subtitle and try again."
        )


def missing_ranges(covered: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
    """Byte ranges of a preallocated file that no stream actually wrote."""
    spans = sorted((max(0, start), min(size, end)) for start, end in covered if end > start)
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < size:
        gaps.append((cursor, size))
    return [(start, end) for start, end in gaps if end > start]


class LargeTelegramTransfer:
    def __init__(self, settings: Settings, *, client=None) -> None:
        self.settings = settings
        self.client = client
        self._started = False
        self._message = None
        self._message_key = None
        self._peer_key = None
        self._peer_value = None
        self._fallback_error: str | None = None

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

    def _worker_count(self, size: int | None) -> int:
        """How many parallel MTProto streams this download deserves."""
        if not size or size < PARALLEL_DOWNLOAD_MIN_BYTES:
            return 1
        return max(1, min(DOWNLOAD_WORKERS, size // DOWNLOAD_CHUNK_BYTES))

    @staticmethod
    def _retry_delay(exc: BaseException, failures: int) -> float | None:
        """How long to wait before resuming a stream, or ``None`` to give up."""
        if failures > DOWNLOAD_RETRIES:
            return None
        seconds = getattr(exc, "seconds", None)
        try:
            requested = float(seconds) if seconds is not None else 0.0
        except (TypeError, ValueError):
            requested = 0.0
        if 0 < requested <= DOWNLOAD_MAX_WAIT:
            return requested
        return min(2.0 ** failures, DOWNLOAD_MAX_BACKOFF)

    async def _pause(self, delay: float) -> None:
        """Backoff between resumed parts; a hook so tests need not really wait."""
        await asyncio.sleep(delay)

    async def _stream(self, message, path: Path, size: int, index: int, stride: int,
                      covered: list[tuple[int, int]]) -> None:
        """One MTProto stream: fixed parts, resuming where it stopped."""
        position = index * DOWNLOAD_CHUNK_BYTES
        failures = 0
        with path.open("r+b") as handle:
            while position < size:
                try:
                    async for piece in self.client.iter_download(
                            message, offset=position, stride=stride,
                            request_size=DOWNLOAD_CHUNK_BYTES, file_size=size):
                        if not piece:
                            return
                        handle.seek(position)
                        handle.write(piece)
                        covered.append((position, position + len(piece)))
                        position += stride
                        failures = 0
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    delay = self._retry_delay(exc, failures)
                    if delay is None:
                        raise
                    logger.warning("MTProto part download failed (%s); resuming in %.1fs",
                                   type(exc).__name__, delay)
                    await self._pause(delay)

    async def _fill_gaps(self, message, path: Path, size: int,
                         covered: list[tuple[int, int]]) -> None:
        """Refetch anything the streams left unwritten, one stream at a time."""
        gaps = missing_ranges(covered, size)
        if not gaps:
            return
        logger.warning("MTProto left %d part(s) unwritten; refetching them", len(gaps))
        with path.open("r+b") as handle:
            for start, end in gaps:
                position = start
                while position < end:
                    progressed = False
                    async for piece in self.client.iter_download(
                            message, offset=position, request_size=DOWNLOAD_CHUNK_BYTES,
                            file_size=size):
                        if not piece:
                            break
                        handle.seek(position)
                        handle.write(piece)
                        position += len(piece)
                        progressed = True
                        if position >= end:
                            break
                    if not progressed:
                        raise OSError("a missing part could not be refetched")

    async def _parallel_download(self, message, path: Path, size: int, workers: int) -> None:
        """Fetch interleaved MTProto parts over ``workers`` connections at once."""
        # Preallocate so each stream can write at its own offset.
        with path.open("wb") as handle:
            handle.truncate(size)
        stride = workers * DOWNLOAD_CHUNK_BYTES
        covered: list[tuple[int, int]] = []
        await asyncio.gather(*(self._stream(message, path, size, index, stride, covered)
                               for index in range(workers)))
        await self._fill_gaps(message, path, size, covered)

    async def _single_stream(self, message, path: Path) -> None:
        saved = await self.client.download_media(message, file=str(path))
        if not saved or not path.is_file():
            raise OSError("download did not create the destination")

    async def _download_to(self, message, path: Path, size: int | None) -> None:
        workers = self._worker_count(size)
        if size is None or workers < 2 or getattr(self.client, "iter_download", None) is None:
            await self._single_stream(message, path)
            return
        try:
            await self._parallel_download(message, path, size, workers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one slow stream is better than failing
            # Several connections are faster but more fragile than Telethon's own
            # single-stream downloader, so fall back to it before giving up.
            logger.warning("Parallel MTProto download failed (%s); using one stream",
                           type(exc).__name__)
            await asyncio.to_thread(path.unlink, missing_ok=True)
            self._fallback_error = type(exc).__name__
            await self._single_stream(message, path)

    def _download_message(self, exc: BaseException) -> str:
        """A safe, actionable failure text; the error class names the cause."""
        detail = type(exc).__name__
        if self._fallback_error:
            detail = f"{self._fallback_error}, then {detail}"
        return ("The Telegram file download failed or was incomplete "
                f"({detail}). Please send the file again and retry.")

    async def download(self, job: TranslationJob, path: Path) -> None:
        message = await self._original(job)
        ensure_telegram_size(message.document.size if message.document else None)
        ensure_telegram_size(job.file_size)
        document_size = message.document.size if message.document else None
        try:
            size = document_size if document_size is not None else job.file_size
            await self._download_to(message, path, size)
            if not path.is_file():
                raise OSError("download did not create the destination")
            actual = path.stat().st_size
            if (actual <= 0 or (size is not None and actual != size)
                    or (job.file_size is not None and actual != job.file_size)):
                logger.warning(
                    "Downloaded %s bytes; document has %s and the update reported %s",
                    actual, document_size, job.file_size)
                raise ValueError("incomplete Telegram download")
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            if exc.errno in _DISK_ERRNOS:
                logger.warning("Telegram download ran out of disk (%s)", exc.errno)
                raise LargeTransferError(
                    "Could not save the Telegram file: the deployment ran out of temporary disk space; "
                    "no partial subtitle was sent."
                ) from None
            logger.warning("Large Telegram download failed (%s)", type(exc).__name__)
            raise LargeTransferError(self._download_message(exc)) from None
        except Exception as exc:  # noqa: BLE001 - may include private data
            logger.warning("Large Telegram download failed (%s)", type(exc).__name__)
            raise LargeTransferError(self._download_message(exc)) from None

    async def send(self, job: TranslationJob, path: Path, filename: str, caption: str) -> None:
        message = await self._original(job)
        ensure_telegram_size(path.stat().st_size)
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

    async def _peer(self, chat_id: int):
        """Resolve a chat once; the log channel is not in the bot's update stream."""
        if self._peer_key == chat_id and self._peer_value is not None:
            return self._peer_value
        try:
            peer = await self.client.get_input_entity(chat_id)
        except Exception:  # noqa: BLE001 - retry after refreshing the dialog cache
            async for _dialog in self.client.iter_dialogs(limit=None):
                pass
            peer = await self.client.get_input_entity(chat_id)
        self._peer_key = chat_id
        self._peer_value = peer
        return peer

    async def send_file_to(self, chat_id: int, path: Path, filename: str, caption: str) -> None:
        """Copy one file to another chat (the log channel) over MTProto."""
        await self.start()
        ensure_telegram_size(path.stat().st_size)
        try:
            peer = await self._peer(chat_id)
            # Upload explicitly so large files stream in 512 KiB chunks.
            handle = await self.client.upload_file(str(path), part_size_kb=512, file_name=filename)
            await self.client.send_file(
                peer, file=handle, force_document=True,
                attributes=[DocumentAttributeFilename(filename)],
                caption=caption, parse_mode="html",
            )
        except asyncio.CancelledError:
            raise
        except LargeTransferError:
            raise
        except Exception as exc:  # noqa: BLE001 - may include chat or caption data
            logger.warning("Large log-channel upload failed (%s)", type(exc).__name__)
            raise LargeTransferError(
                "Telegram could not copy the file to the log channel."
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
        self._peer_key = None
        self._peer_value = None
