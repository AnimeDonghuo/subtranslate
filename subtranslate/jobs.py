"""A bounded, in-memory, single-worker queue for a small Koyeb instance."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)
# How often a waiting worker re-checks free memory before taking another job.
ADMISSION_RETRY_SECONDS = 5.0


def available_memory_mb() -> int | None:
    """Free RAM from /proc/meminfo; None when the platform does not expose it."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


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
    # Sender details are used only for /stats, /leaderboard and the log channel.
    user_id: int | None = None
    user_name: str = ""
    username: str = ""
    # Per-process number shown by /status and /cancel_id; 0 means unnumbered.
    task_id: int = 0

    @property
    def key(self) -> tuple[int, int, str]:
        return self.chat_id, self.original_message_id, self.language_code


class EnqueueResult(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "full"


class JobQueue:
    """A bounded queue with up to ``max_concurrent`` workers.

    Each worker takes a job only while the current limit allows it. When the
    host is short of memory, ``current_limit`` drops to one, so the extra
    workers leave their jobs in the queue instead of running a second browser
    on a small free instance.
    """

    def __init__(self, processor: Callable[[TranslationJob], Awaitable[None]], max_waiting: int = 2,
                 max_concurrent: int = 2, min_free_mb: int = 250,
                 free_mb: Callable[[], int | None] | None = None) -> None:
        self._processor = processor
        self._queue: asyncio.Queue[TranslationJob] = asyncio.Queue(maxsize=max_waiting)
        self._pending: set[tuple[int, int, str]] = set()
        self._workers: list[asyncio.Task] = []
        self._active: list[TranslationJob] = []
        self._max_concurrent = max(1, min(4, max_concurrent))
        self._min_free_mb = max(0, min_free_mb)
        self._free_mb = free_mb or available_memory_mb

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def active_jobs(self) -> tuple[TranslationJob, ...]:
        return tuple(self._active)

    def current_limit(self) -> int:
        """Run one job at a time while the host is short of free memory."""
        if self._max_concurrent <= 1:
            return 1
        free = self._free_mb()
        # A host that does not report memory keeps the configured limit.
        return 1 if free is not None and free < self._min_free_mb else self._max_concurrent

    def start(self) -> None:
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._run(slot), name=f"subtitle-worker-{slot}")
                for slot in range(self._max_concurrent)
            ]

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

    def cancel_job(self, key: tuple[int, int, str]) -> bool:
        """Remove one queued job by key, for /cancel_id; returns whether it was queued."""
        removed = False
        retained = []
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if not removed and job.key == key:
                self._pending.discard(job.key)
                self._queue.task_done()
                removed = True
            else:
                retained.append(job)
        for job in retained:
            self._queue.put_nowait(job)
            self._queue.task_done()
        return removed

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

    async def _run(self, slot: int) -> None:
        while True:
            # Wait before dequeuing, so a job under memory pressure stays in
            # the queue where /cancel and /cancel_id can still reach it.
            await self._wait_for_slot(slot)
            job = await self._queue.get()
            self._active.append(job)
            try:
                await self._processor(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                # Don't log subtitle contents, filenames, file URLs or tokens.
                logger.error("Subtitle worker failed (%s)", type(exc).__name__)
            finally:
                if job in self._active:
                    self._active.remove(job)
                self._pending.discard(job.key)
                self._queue.task_done()

    async def _wait_for_slot(self, slot: int) -> None:
        """Only the first slot runs while the host is short of memory."""
        while slot >= self.current_limit():
            await asyncio.sleep(ADMISSION_RETRY_SECONDS)

    async def close(self) -> list[TranslationJob]:
        """Stop all workers and return the active and waiting jobs for notification."""
        # No await between reading the active list and cancelling, so a worker
        # cannot finish a different job in between these operations.
        interrupted = list(self._active)
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._active.clear()
        while not self._queue.empty():
            job = self._queue.get_nowait()
            interrupted.append(job)
            self._pending.discard(job.key)
            self._queue.task_done()
        return interrupted
