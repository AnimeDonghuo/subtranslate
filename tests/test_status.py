"""/status, /cancel_id and /broadcast are exercised without any network access."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from support import (
    FakeBot,
    FakeMessage,
    FakeQuery,
    context,
    make_service,
    make_update,
    user,
)

from subtranslate import menus
from subtranslate.admin import broadcast_command
from subtranslate.jobs import TranslationJob
from subtranslate.progress import STAGES
from subtranslate.store import Database
from subtranslate.subtitles import SubtitleError
from subtranslate.tasks import TaskRegistry, bar, render_task


def running_progress(stage="starting the browser", language="Bengali", completed=0, total=0):
    return SimpleNamespace(stage=stage, language=language, batch_completed=completed, batch_total=total)


def test_single_task_block_matches_the_requested_layout():
    registry = TaskRegistry()
    task = registry.register(job_key=(42, 7, "hi"), chat_id=42, user_id=42, user_name="Asha Kumari",
                             username="asha", file_name="movie.srt", language="Bengali")
    registry.mark_running(task.id, asyncio.Event(), now=100.0, progress=running_progress())
    text = render_task(task, now=142.0)
    assert text.splitlines()[0] == "🔄 <b>Task #1</b> · translation in progress"
    assert "👤 Asha Kumari · 🆔 <code>42</code> · @asha" in text
    assert "Translating to Bengali" in text
    assert "[■■□□□□□□□□] Step 2/10" in text
    assert STAGES[1][1] == "Preparing the browser" and "Stage: Preparing the browser" in text
    assert "Elapsed: 0:42" in text
    assert text.endswith("The bar shows stages, not the website's translation percentage.")
    # A single-language task never shows the batch block.
    assert "Batch:" not in text


def test_batch_task_block_adds_the_languages_bar():
    registry = TaskRegistry()
    task = registry.register(job_key=(42, 7, "*"), chat_id=42, user_id=42, user_name="Asha",
                             username="asha", file_name="movie.srt", language="all languages",
                             is_batch=True, batch_total=5)
    registry.mark_running(task.id, asyncio.Event(), now=100.0,
                          progress=running_progress(completed=3, total=5, language="Bengali"))
    text = render_task(task, now=100.0)
    assert "Batch: 3/5 languages done" in text
    assert "[■■■■■■□□□□]" in text
    assert "Translating to Bengali" in text
    assert "[■■□□□□□□□□] Step 2/10" in text


def test_queued_task_block_and_escaping():
    registry = TaskRegistry()
    task = registry.register(job_key=(42, 7, "hi"), chat_id=42, user_id=42,
                             user_name="<b>Asha</b>", username="a&b", file_name="<movie>.srt",
                             language="Bengali")
    text = render_task(task, now=10.0)
    assert "🕓 <b>Task #1</b> · waiting in queue" in text
    assert "&lt;b&gt;Asha&lt;/b&gt;" in text and "a&amp;b" in text and "&lt;movie&gt;.srt" in text
    assert "/cancel_id 1" in text
    # HTML in a filename can never inject markup.
    assert "<movie>" not in text


def test_bar_and_elapsed_helpers():
    assert bar(0, 10) == "□□□□□□□□□□"
    assert bar(5, 10) == "■■■■■□□□□□"
    assert bar(10, 10) == "■■■■■■■■■■"
    assert bar(3, 0) == "■■■■■■■■■■"
    from subtranslate.tasks import format_elapsed

    assert format_elapsed(42) == "0:42"
    assert format_elapsed(3_725) == "1:02:05"


async def test_status_lists_only_own_tasks_but_an_owner_sees_everything():
    service = make_service(owner=1)
    service.bot = FakeBot()
    first = service.tasks.register(job_key=(42, 1, "hi"), chat_id=42, user_id=42, user_name="Asha",
                                   username="asha", file_name="a.srt", language="Hindi")
    service.tasks.mark_running(first.id, asyncio.Event(), now=0.0)
    second = service.tasks.register(job_key=(43, 2, "es"), chat_id=43, user_id=43, user_name="Other",
                                    username="other", file_name="b.srt", language="Spanish")

    update = make_update(user_id=42)
    await menus.send_status(service, update, None)
    own = update.effective_message.reply_text.await_args.args[0]
    assert "Your active translations" in own and "Task #1" in own
    assert "Task #2" not in own and "Other" not in own

    owner = make_update(user_id=1)
    await menus.send_status(service, owner, None)
    everything = owner.effective_message.reply_text.await_args.args[0]
    assert "All active translations" in everything
    assert "Task #1" in everything and "Task #2" in everything
    assert "waiting in queue" in everything and "/cancel_id 2" in everything
    assert second.state == "queued"


async def test_status_without_tasks_explains_the_commands():
    service = make_service(owner=1)
    update = make_update(user_id=42)
    await menus.send_status(service, update, None)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "No active translation" in text and "/status" in text and "/cancel_id" in text


async def test_cancel_id_stops_one_running_task_only():
    service = make_service(owner=1)
    service.bot = FakeBot()
    event = asyncio.Event()
    task = service.tasks.register(job_key=(42, 1, "hi"), chat_id=42, user_id=42, language="Hindi")
    service.tasks.mark_running(task.id, event, now=0.0)
    other = service.tasks.register(job_key=(42, 2, "es"), chat_id=42, user_id=42, language="Spanish")

    update = make_update(user_id=42)
    await menus.cancel_task(service, update, context("1"))
    assert event.is_set()
    assert not other.cancel.is_set()
    assert "Stopping task #1" in update.effective_message.reply_text.await_args.args[0]


async def test_cancel_id_removes_a_queued_task_from_the_queue():
    service = make_service(owner=1)
    job = TranslationJob(42, 1, "file", "movie.srt", 10, "hi", "Hindi", user_id=42, task_id=1)
    service.tasks.register(job_key=job.key, chat_id=42, user_id=42, language="Hindi")
    assert service.queue.submit(job).value == "accepted"
    update = make_update(user_id=42)
    await menus.cancel_task(service, update, context("1"))
    assert "removed from the queue" in update.effective_message.reply_text.await_args.args[0]
    assert service.queue._queue.empty() and service.tasks.get(1) is None


async def test_cancel_id_is_restricted_to_the_task_owner_unless_they_are_the_owner():
    service = make_service(owner=1)
    service.bot = FakeBot()
    event = asyncio.Event()
    task = service.tasks.register(job_key=(43, 1, "hi"), chat_id=43, user_id=43, user_name="Other",
                                  language="Hindi")
    service.tasks.mark_running(task.id, event, now=0.0)

    stranger = make_update(user_id=42)
    await menus.cancel_task(service, stranger, context("1"))
    assert not event.is_set()
    assert "another user" in stranger.effective_message.reply_text.await_args.args[0]

    unknown = make_update(user_id=42)
    await menus.cancel_task(service, unknown, context("99"))
    assert "No task #99" in unknown.effective_message.reply_text.await_args.args[0]

    invalid = make_update(user_id=42)
    await menus.cancel_task(service, invalid, context("abc"))
    assert "Usage: /cancel_id" in invalid.effective_message.reply_text.await_args.args[0]

    owner = make_update(user_id=1)
    await menus.cancel_task(service, owner, context("#1"))
    assert event.is_set()
    # The task's owner is told that the bot owner stopped it.
    assert any(message["chat_id"] == 43 and "bot owner" in message["text"] for message in service.bot.messages)


async def test_each_queued_job_gets_a_number_and_a_duplicate_frees_it_again():
    service = make_service(owner=1)
    document = SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=120)
    message = FakeMessage(chat_id=42, message_id=100)
    message.reply_to_message = SimpleNamespace(
        message_id=77, from_user=SimpleNamespace(id=42), document=document,
    )
    query = FakeQuery("lang:hi", 42, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=user(42))
    await service.callback(update, None)
    job = service.queue._queue.get_nowait()
    service.queue._queue.task_done()
    assert job.task_id == 1
    assert service.tasks.get(1).job_key == job.key
    assert "task #1" in message.reply_text.await_args.args[0]

    # Submitting the same message/language again is a duplicate: its new number is dropped.
    await service.callback(update, None)
    assert len(service.tasks.all()) == 1
    assert query.answers[-1][0] == "That translation is already queued or running."


async def test_broadcast_reaches_users_skips_bans_and_reports_counts():
    service = make_service(owner=1)
    service.bot = FakeBot()
    for user_id in (42, 43, 44):
        await service.store.touch_user(user_id, f"User {user_id}", f"u{user_id}")
    await service.store.ban(43)
    service.bot.fail_chat_ids.add(44)

    update = make_update(user_id=1)
    ctx = SimpleNamespace(args=["hello", "everyone"], bot=service.bot)
    await broadcast_command(service, update, ctx)

    delivered = [message["chat_id"] for message in service.bot.messages
                 if message["text"] == "hello everyone" and "message_id" not in message]
    assert 42 in delivered and 1 in delivered          # owner is included
    assert 43 not in delivered                          # banned user is skipped
    summary = [message["text"] for message in service.bot.messages if "Broadcast finished" in message["text"]]
    assert summary and "Sent: 2" in summary[-1] and "Failed: 1" in summary[-1]
    assert "Skipped (banned): 1" in summary[-1]
    # The broadcast text is sent verbatim, not parsed as HTML.
    assert all(message.get("parse_mode") is None for message in service.bot.messages
               if message["text"] == "hello everyone")


async def test_broadcast_is_owner_only_and_needs_text():
    service = make_service(owner=1)
    service.bot = FakeBot()
    stranger = make_update(user_id=42)
    await broadcast_command(service, stranger, SimpleNamespace(args=["hi"], bot=service.bot))
    assert "only for the bot owner" in stranger.effective_message.reply_text.await_args.args[0]

    owner = make_update(user_id=1)
    await broadcast_command(service, owner, SimpleNamespace(args=[], bot=service.bot))
    assert "Usage: /broadcast" in owner.effective_message.reply_text.await_args.args[0]


async def test_known_user_ids_include_mongo_and_memory_rows():
    store = Database()
    assert await store.all_user_ids() == ()
    await store.touch_user(888, "Name", "handle")
    await store.touch_user(999, "Other", "")
    assert sorted(await store.all_user_ids()) == [888, 999]


async def test_a_finished_job_marks_running_then_releases_its_number(monkeypatch):
    service = make_service(owner=1)
    service.bot = FakeBot()
    job = TranslationJob(42, 1, "file", "movie.srt", 10, "hi", "Hindi", user_id=42,
                         user_name="Asha", username="asha")
    task = service.tasks.register(job_key=job.key, chat_id=42, user_id=42, user_name="Asha",
                                  username="asha", file_name="movie.srt", language="Hindi")
    job = replace(job, task_id=task.id)
    seen = {}

    async def fail(*_args, **_kwargs):
        seen["state"] = service.tasks.get(task.id).state
        seen["stage"] = service.tasks.get(task.id).stage
        raise SubtitleError("bad file")

    monkeypatch.setattr(service, "_download_source", fail)
    await service.process_job(job)

    assert seen == {"state": "running", "stage": "downloading the original"}
    assert service.tasks.get(task.id) is None
    assert any("Could not translate: bad file" in message["text"] for message in service.bot.messages)
