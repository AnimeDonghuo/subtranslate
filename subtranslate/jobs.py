"""A bounded, in-memory, single-worker queue for a small Koyeb instance."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationJob:
    chat_id: int
    original_message_id: int
    file_id: str
    file_name: str
    file_size: int | None
    language_code: str
    language_name: str
    languages: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> tuple[int, int, str]:
        return self.chat_id, self.original_message_id, self.language_code


class EnqueueResult(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "full"


class JobQueue:
    def __init__(self, processor: Callable[[TranslationJob], Awaitable[None]], max_waiting: int = 2) -> None:
        self._processor = processor
        self._queue: asyncio.Queue[TranslationJob] = asyncio.Queue(maxsize=max_waiting)
        self._pending: set[tuple[int, int, str]] = set()
        self._worker: asyncio.Task | None = None
        self._active: TranslationJob | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="subtitle-worker")

    def submit(self, job: TranslationJob) -> EnqueueResult:
        # No await between the check and put, so this operation is atomic on
        # the event loop. Only metadata is queued, not subtitle contents.
        if job.key in self._pending:
            return EnqueueResult.DUPLICATE
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return EnqueueResult.FULL
        self._pending.add(job.key)
        return EnqueueResult.ACCEPTED

    def cancel_waiting_batches(self, chat_id: int) -> int:
        """Remove this user's queued all-language jobs without stopping others."""
        return self._cancel_waiting(chat_id, batches_only=True)

    def cancel_waiting_jobs(self, chat_id: int) -> int:
        """Remove all this user's queued jobs without stopping other users."""
        return self._cancel_waiting(chat_id, batches_only=False)

    def _cancel_waiting(self, chat_id: int, *, batches_only: bool) -> int:
        retained = []
        removed = 0
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job.chat_id == chat_id and (job.languages or not batches_only):
                self._pending.discard(job.key)
                self._queue.task_done()
                removed += 1
            else:
                retained.append(job)
        for job in retained:
            # Requeue before marking the old entry done, so join() cannot be
            # released early while there is retained work waiting.
            self._queue.put_nowait(job)
            self._queue.task_done()
        return removed

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._active = job
            try:
                await self._processor(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the single worker alive
                # Don't log subtitle contents, filenames, file URLs or tokens.
                logger.error("Subtitle worker failed (%s)", type(exc).__name__)
            finally:
                self._active = None
                self._pending.discard(job.key)
                self._queue.task_done()

    async def close(self) -> list[TranslationJob]:
        """Stop work and return the active and waiting jobs for notification."""
        interrupted = []
        if self._worker is not None:
            # No await between reading _active and cancelling, so the worker
            # cannot finish a different job in between these operations.
            if self._active is not None:
                interrupted.append(self._active)
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        while not self._queue.empty():
            job = self._queue.get_nowait()
            interrupted.append(job)
            self._pending.discard(job.key)
            self._queue.task_done()
        return interrupted
