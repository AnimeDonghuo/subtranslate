"""Temporary files must outlive any worker that still uses them."""

import asyncio
import threading

import pytest

from subtranslate.fileio import run_file_io, run_scan


async def test_cancelled_scan_waits_for_the_thread_to_stop(tmp_path):
    started, exited = threading.Event(), threading.Event()
    path = tmp_path / "in-use"
    path.write_text("data")

    def scan(path, stop):
        started.set()
        assert stop.wait(5)
        # The owner must not remove the job's files until this worker exits.
        assert path.read_text() == "data"
        exited.set()

    task = asyncio.create_task(run_scan(scan, path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


async def test_repeated_cancellation_still_waits_for_the_scan_thread(tmp_path):
    started, stopping, release, exited = (threading.Event() for _ in range(4))
    path = tmp_path / "in-use"
    path.write_text("data")

    def scan(path, stop):
        started.set()
        assert stop.wait(5)
        stopping.set()
        assert release.wait(5)
        assert path.read_text() == "data"
        exited.set()

    task = asyncio.create_task(run_scan(scan, path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    assert await asyncio.to_thread(stopping.wait, 2)
    task.cancel()  # e.g. shutdown follows the user's /cancel command
    await asyncio.sleep(0.01)
    still_waiting = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert still_waiting and exited.is_set()


async def test_shutdown_during_user_cancel_waits_for_active_scan(tmp_path):
    from subtranslate.bot import BotService

    started, stopping, release, exited = (threading.Event() for _ in range(4))
    path = tmp_path / "in-use"
    path.write_text("data")
    user_cancel = asyncio.Event()

    def scan(path, stop):
        started.set()
        assert stop.wait(5)
        stopping.set()
        assert release.wait(5)
        assert path.read_text() == "data"
        exited.set()

    task = asyncio.create_task(BotService._cancelable(run_scan(scan, path), user_cancel))
    assert await asyncio.to_thread(started.wait, 2)
    user_cancel.set()
    assert await asyncio.to_thread(stopping.wait, 2)
    task.cancel()  # graceful shutdown while /cancel is already cleaning up
    await asyncio.sleep(0.01)
    still_waiting = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert still_waiting and exited.is_set()


async def test_cancelled_short_file_operation_finishes_before_returning(tmp_path):
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    path = tmp_path / "in-use"
    path.write_text("data")

    def operation(path):
        started.set()
        assert release.wait(5)
        assert path.read_text() == "data"
        exited.set()

    task = asyncio.create_task(run_file_io(operation, path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
