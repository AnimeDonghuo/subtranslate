import asyncio
from dataclasses import replace

from subtranslate.jobs import EnqueueResult, JobQueue, TranslationJob


def job(number: int) -> TranslationJob:
    return TranslationJob(1, number, "file", "test.srt", 100, "hi", "Hindi")


async def test_queue_is_bounded_deduplicated_and_serial():
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0
    done = []

    async def process(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        await release.wait()
        done.append(item.original_message_id)
        active -= 1

    # Serial semantics for this test; concurrency is covered in test_queue.py.
    queue = JobQueue(process, max_waiting=2, max_concurrent=1, free_mb=lambda: 4000)
    queue.start()
    assert queue.submit(job(1)) is EnqueueResult.ACCEPTED
    await asyncio.wait_for(started.wait(), 1)
    assert queue.submit(job(1)) is EnqueueResult.DUPLICATE
    assert queue.submit(job(2)) is EnqueueResult.ACCEPTED
    assert queue.submit(job(3)) is EnqueueResult.ACCEPTED
    assert queue.submit(job(4)) is EnqueueResult.FULL
    release.set()
    await asyncio.wait_for(queue._queue.join(), 1)
    assert done == [1, 2, 3]
    assert peak == 1
    assert queue.submit(replace(job(1), language_code="fr")) is EnqueueResult.ACCEPTED
    await asyncio.wait_for(queue._queue.join(), 1)
    assert await queue.close() == []


async def test_close_reports_active_and_waiting_jobs_once():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(item):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    queue = JobQueue(process)
    queue.start()
    assert queue.submit(job(1)) is EnqueueResult.ACCEPTED
    await asyncio.wait_for(started.wait(), 1)
    assert queue.submit(job(2)) is EnqueueResult.ACCEPTED
    assert queue.submit(job(3)) is EnqueueResult.ACCEPTED
    interrupted = await queue.close()
    assert cancelled.is_set()
    assert [item.original_message_id for item in interrupted] == [1, 2, 3]
    assert await queue.close() == []
    await asyncio.wait_for(queue._queue.join(), 1)
