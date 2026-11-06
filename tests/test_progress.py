import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TelegramError

from subtranslate.progress import ProgressMessage


async def test_one_status_message_shows_actual_stage_and_finishes():
    edited = asyncio.Event()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=17)))

    async def edit(**kwargs):
        edited.set()

    bot.edit_message_text = AsyncMock(side_effect=edit)
    progress = await ProgressMessage.start(bot, 42, "Hindi", edit_interval=0.01, heartbeat_interval=20)
    assert progress is not None
    try:
        progress.set_stage("opening the subtitle site")
        progress.set_stage("uploading and parsing the subtitle")
        progress.set_stage("translating")
        await asyncio.wait_for(edited.wait(), 1)
        call = bot.edit_message_text.await_args.kwargs
        assert call["message_id"] == 17 and call["chat_id"] == 42
        assert "[■■■■■■□□□□] Step 6/10" in call["text"]
        assert "Stage: Translating on the website" in call["text"]
        assert "not the website's translation percentage" in call["text"]
        # Several rapid stage changes were coalesced into one message edit.
        assert bot.edit_message_text.await_count == 1
        assert await progress.finish("✅ Hindi complete.")
        assert bot.edit_message_text.await_args.kwargs["text"] == "✅ Hindi complete."
        bot.send_message.assert_awaited_once()
    finally:
        await progress.close()


async def test_batch_bar_counts_only_processed_languages():
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=18)),
                          edit_message_text=AsyncMock())
    progress = await ProgressMessage.start(bot, 42, "all languages", batch_total=4, edit_interval=0.01)
    assert progress is not None
    try:
        progress.set_language("Spanish", current=2, completed=1)
        progress.set_stage("uploading and parsing the subtitle")
        text = progress.render()
        assert "Batch: 1/4 languages processed" in text
        assert "[■■□□□□□□□□]" in text
        assert "Current: Spanish (2/4)" in text
        assert "Step 4/10" in text and "Uploading and checking" in text
        progress.set_completed(2)
        assert "Batch: 2/4 languages processed" in progress.render()
        assert await progress.finish("Finished: 2 file(s) sent, 0 unavailable, 0 failed.")
    finally:
        await progress.close()


async def test_failed_status_edits_do_not_raise_or_leak_secrets(caplog):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=19)),
                          edit_message_text=AsyncMock(side_effect=TelegramError("DO_NOT_LOG_TOKEN")))
    progress = await ProgressMessage.start(bot, 42, "Hindi", edit_interval=0)
    assert progress is not None
    try:
        progress.set_stage("translating")
        assert not await progress.finish("Translation failed.")
        assert "DO_NOT_LOG_TOKEN" not in caplog.text
    finally:
        await progress.close()


async def test_hanging_edit_is_bounded_and_terminal_update_can_recover():
    first_started = asyncio.Event()
    attempts = 0

    async def edit(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_started.set()
            await asyncio.Event().wait()

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
                          edit_message_text=AsyncMock(side_effect=edit))
    progress = await ProgressMessage.start(bot, 42, "Hindi", edit_interval=0.01, edit_timeout=0.05)
    assert progress is not None
    try:
        progress.set_stage("translating")
        await asyncio.wait_for(first_started.wait(), 1)
        assert await asyncio.wait_for(progress.finish("Finished."), 1)
        assert attempts == 2
        assert bot.edit_message_text.await_args.kwargs["text"] == "Finished."
    finally:
        await progress.close()
    assert progress._task.done()
