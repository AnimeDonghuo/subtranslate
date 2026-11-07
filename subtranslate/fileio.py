"""Short file operations that must finish before temporary files are removed."""

import asyncio
import threading


async def wait_for_cleanup(task):
    """Finish a task before propagating even repeated external cancellations."""
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
        except Exception:  # noqa: BLE001 - the original cancellation takes precedence
            break
    if not task.cancelled():
        task.exception()  # retrieve any cleanup failure without masking cancellation
    if canceled:
        raise asyncio.CancelledError


async def run_scan(function, *args):
    """Cooperatively stop a potentially long, cue-by-cue disk operation."""
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await wait_for_cleanup(task)
        raise


async def run_file_io(function, *args):
    # Cancelling to_thread alone does not stop its worker. Wait for this
    # bounded operation before its owning job closes/removes the files.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await wait_for_cleanup(task)
        raise
