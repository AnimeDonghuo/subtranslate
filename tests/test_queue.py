"""Concurrency: two jobs at most, and one while the host is short of memory."""

import asyncio

from subtranslate import jobs as jobs_module
from subtranslate.jobs import EnqueueResult, JobQueue, TranslationJob


def make_job(number: int, chat_id: int = 1) -> TranslationJob:
    return TranslationJob(chat_id, number, "file", "movie.srt", 10, "hi", "Hindi", user_id=chat_id)


def test_current_limit_follows_free_memory():
    healthy = JobQueue(lambda job: None, max_concurrent=2, min_free_mb=250, free_mb=lambda: 900)
    tight = JobQueue(lambda job: None, max_concurrent=2, min_free_mb=250, free_mb=lambda: 120)
    unknown = JobQueue(lambda job: None, max_concurrent=2, min_free_mb=250, free_mb=lambda: None)
    single = JobQueue(lambda job: None, max_concurrent=1, free_mb=lambda: 900)
    assert healthy.current_limit() == 2
    assert tight.current_limit() == 1
    assert unknown.current_limit() == 2       # platforms without /proc keep 2
    assert single.current_limit() == 1
    assert healthy.max_concurrent == 2


async def test_two_jobs_run_together_and_the_third_waits(monkeypatch):
    monkeypatch.setattr(jobs_module, "ADMISSION_RETRY_SECONDS", 0.01)
    running = set()
    peak = 0
    both_started = asyncio.Event()
    processed = []

    async def processor(job):
        nonlocal peak
        running.add(job.original_message_id)
        peak = max(peak, len(running))
        if len(running) == 2:
            both_started.set()
        await asyncio.sleep(0.05)
        processed.append(job.original_message_id)
        running.discard(job.original_message_id)

    queue = JobQueue(processor, max_waiting=2, max_concurrent=2, min_free_mb=0, free_mb=lambda: 4000)
    queue.start()
    try:
        first, second = make_job(1), make_job(2)
        assert queue.submit(first) is EnqueueResult.ACCEPTED
        assert queue.submit(second) is EnqueueResult.ACCEPTED
        await asyncio.wait_for(both_started.wait(), 2)
        assert len(queue.active_jobs) == 2
        assert queue.submit(make_job(3)) is EnqueueResult.ACCEPTED  # waits in the queue
        await asyncio.wait_for(queue._queue.join(), 5)
        assert peak == 2
        assert sorted(processed) == [1, 2, 3]
    finally:
        await queue.close()


async def test_low_memory_makes_the_second_slot_wait(monkeypatch):
    monkeypatch.setattr(jobs_module, "ADMISSION_RETRY_SECONDS", 0.01)
    running = 0
    peak = 0
    release = asyncio.Event()
    processed = []

    async def processor(job):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        processed.append(job.original_message_id)
        running -= 1

    queue = JobQueue(processor, max_waiting=2, max_concurrent=2, min_free_mb=250, free_mb=lambda: 90)
    queue.start()
    try:
        assert queue.submit(make_job(1)) is EnqueueResult.ACCEPTED
        for _ in range(200):
            if processed == [] and running == 1:
                break
            await asyncio.sleep(0.01)
        assert running == 1
        # The second job stays in the queue while memory is short.
        assert queue.submit(make_job(2)) is EnqueueResult.ACCEPTED
        await asyncio.sleep(0.1)
        assert running == 1 and queue._queue.qsize() == 1
        release.set()
        await asyncio.wait_for(queue._queue.join(), 5)
        assert peak == 1 and sorted(processed) == [1, 2]
    finally:
        await queue.close()


async def test_close_stops_every_worker_and_reports_waiting_jobs():
    started = asyncio.Event()

    async def processor(job):
        started.set()
        await asyncio.sleep(30)

    queue = JobQueue(processor, max_waiting=2, max_concurrent=2, min_free_mb=0, free_mb=lambda: 4000)
    queue.start()
    queue.submit(make_job(1))
    await asyncio.wait_for(started.wait(), 2)
    queue.submit(make_job(2))
    interrupted = await queue.close()
    assert [job.original_message_id for job in interrupted] == [1, 2]
    assert queue.active_jobs == ()


async def test_cancel_id_can_remove_a_queued_job_by_key():
    queue = JobQueue(lambda job: None, max_waiting=2, max_concurrent=1, free_mb=lambda: 4000)
    job = make_job(7)
    assert queue.submit(job) is EnqueueResult.ACCEPTED
    assert queue.cancel_job(job.key) is True
    assert queue._queue.empty()
    assert queue.cancel_job(job.key) is False


def test_cgroup_and_host_memory_helpers(monkeypatch, tmp_path):
    mib = 1024 * 1024
    healthy = tmp_path / "healthy"
    healthy.write_text(f"{600 * mib}\n")
    used = tmp_path / "used"
    used.write_text(f"{100 * mib}\n")
    unlimited = tmp_path / "unlimited"
    unlimited.write_text("max\n")
    missing = tmp_path / "missing"

    monkeypatch.setattr(jobs_module, "CGROUP_PAIRS", ((str(healthy), str(used)),))
    assert jobs_module.cgroup_available_mb() == 500

    monkeypatch.setattr(jobs_module, "CGROUP_PAIRS", ((str(unlimited), str(used)),))
    assert jobs_module.cgroup_available_mb() is None       # v2 "max" = no limit

    monkeypatch.setattr(jobs_module, "CGROUP_PAIRS", ((str(missing), str(used)),))
    assert jobs_module.cgroup_available_mb() is None       # not a container

    assert jobs_module._available_mb(1 << 62, 1) is None   # cgroup v1 "unlimited"
    assert jobs_module._available_mb(0, 0) is None
    assert jobs_module._available_mb(None, 5) is None
    assert jobs_module._available_mb(100 * 1024 * 1024, 20 * 1024 * 1024) == 80


def test_available_memory_uses_the_tighter_container_limit(monkeypatch):
    monkeypatch.setattr(jobs_module, "cgroup_available_mb", lambda: 300)
    monkeypatch.setattr(jobs_module, "host_available_mb", lambda: 8000)
    assert jobs_module.available_memory_mb() == 300
    monkeypatch.setattr(jobs_module, "cgroup_available_mb", lambda: None)
    assert jobs_module.available_memory_mb() == 8000
    monkeypatch.setattr(jobs_module, "host_available_mb", lambda: None)
    assert jobs_module.available_memory_mb() is None
