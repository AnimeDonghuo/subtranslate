"""Free limits: 10 presets, a locked select-all button, and owner-granted premium."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from support import (
    FakeBot,
    FakeMessage,
    FakeQuery,
    context,
    make_query_update,
    make_service,
    make_update,
    user,
)
from telegram.error import RetryAfter

from subtranslate import admin as admin_module
from subtranslate import menus
from subtranslate.config import FREE_PRESET_LIMIT
from subtranslate.store import Database

DOC = SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=120)


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_premium_grant_extends_and_can_be_revoked():
    store = Database()
    assert await store.is_premium(42) is False
    first = await store.grant_premium(42, 2)
    assert first > datetime.now(UTC)
    assert await store.is_premium(42) is True
    extended = await store.grant_premium(42, 3)
    assert extended > first                       # extensions stack, not reset
    assert await store.revoke_premium(42) is True
    assert await store.is_premium(42) is False
    assert await store.revoke_premium(42) is False


async def test_owners_are_premium_and_premium_unlocks_the_picker():
    service = make_service(owner=1)
    assert await service.is_premium(1) is True
    assert await service.is_premium(42) is False

    free = make_update(user_id=42, document=DOC)
    await service.document(free, None)
    free_buttons = buttons(free.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert any(button.callback_data == "all:locked" for button in free_buttons)
    assert "🔒 Select all languages · premium" in [button.text for button in free_buttons]

    await service.store.grant_premium(42, 1)
    paid = make_update(user_id=42, document=DOC)
    await service.document(paid, None)
    paid_buttons = buttons(paid.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert any(button.callback_data == "all:confirm:0" for button in paid_buttons)
    assert all(button.callback_data != "all:locked" for button in paid_buttons)

    owner = make_update(user_id=1, document=DOC)
    await service.document(owner, None)
    assert any(button.callback_data == "all:confirm:0"
               for button in buttons(owner.effective_message.reply_text.await_args.kwargs["reply_markup"]))


async def test_select_all_is_refused_without_premium():
    service = make_service(owner=1)
    for data in ("all:locked", "all:confirm:1", "all:start"):
        update, query = make_query_update(data, user_id=42)
        await service.callback(update, None)
        assert "premium" in (query.answers[-1][0] or "").lower()
        assert service.queue._queue.empty()


async def test_select_all_runs_for_a_premium_user():
    service = make_service(owner=1)
    await service.store.grant_premium(42, 1)
    message = FakeMessage(chat_id=42, message_id=100)
    message.reply_to_message = SimpleNamespace(
        message_id=77, from_user=SimpleNamespace(id=42), document=DOC,
    )
    query = FakeQuery("all:start", 42, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=user(42))
    await service.callback(update, None)
    job = service.queue._queue.get_nowait()
    service.queue._queue.task_done()
    assert len(job.languages) == len(service.catalog.items())


async def test_free_users_can_save_at_most_ten_presets():
    service = make_service(owner=1)
    codes = [code for code, _name in service.catalog.items()][:FREE_PRESET_LIMIT]
    await service.store.set_presets(42, codes)

    update, query = make_query_update("us:open", user_id=42)
    await menus.settings_callback(service, update, context())
    assert "up to <b>10</b>" in query.edits[-1]["text"]

    eleventh = [code for code, _name in service.catalog.items()][FREE_PRESET_LIMIT]
    update, query = make_query_update(f"us:t:{eleventh}", user_id=42)
    await menus.settings_callback(service, update, context())
    assert "up to 10" in query.answers[-1][0]
    assert eleventh not in service.preset_draft(42)
    assert len(service.preset_draft(42)) == FREE_PRESET_LIMIT

    # Saving also trims defensively if a draft grew past the limit.
    service.preset_draft(42).add(eleventh)
    update, query = make_query_update("us:save", user_id=42)
    await menus.settings_callback(service, update, context())
    assert len(await service.store.presets(42)) == FREE_PRESET_LIMIT
    assert eleventh not in await service.store.presets(42)


async def test_premium_users_can_select_more_than_ten_presets():
    service = make_service(owner=1)
    await service.store.grant_premium(42, 1)
    codes = [code for code, _name in service.catalog.items()][:FREE_PRESET_LIMIT]
    await service.store.set_presets(42, codes)
    eleventh = [code for code, _name in service.catalog.items()][FREE_PRESET_LIMIT]

    update, query = make_query_update(f"us:t:{eleventh}", user_id=42)
    await menus.settings_callback(service, update, context())
    assert eleventh in service.preset_draft(42)
    assert "premium" in query.edits[-1]["text"].lower()


async def test_expired_premium_only_runs_the_first_ten_saved_languages():
    service = make_service(owner=1)
    codes = [code for code, _name in service.catalog.items()][:FREE_PRESET_LIMIT + 2]
    await service.store.set_presets(42, codes)
    message = FakeMessage(chat_id=42, message_id=100)
    message.reply_to_message = SimpleNamespace(
        message_id=77, from_user=SimpleNamespace(id=42), document=DOC,
    )
    query = FakeQuery("preset:start", 42, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=user(42))
    await service.callback(update, None)
    job = service.queue._queue.get_nowait()
    service.queue._queue.task_done()
    assert len(job.languages) == FREE_PRESET_LIMIT
    assert any("up to 10" in (answer[0] or "") for answer in query.answers)


async def test_addpremium_and_removepremium_commands():
    service = make_service(owner=1)
    service.bot = FakeBot()
    owner_update = make_update(user_id=1)
    await admin_module.addpremium_command(service, owner_update, context("42", "7"))
    assert await service.store.is_premium(42) is True
    assert "Premium enabled" in owner_update.effective_message.reply_text.await_args.args[0]
    assert any(message["chat_id"] == 42 and "Premium enabled" in message["text"]
               for message in service.bot.messages)

    removed = make_update(user_id=1)
    await admin_module.removepremium_command(service, removed, context("42"))
    assert await service.store.is_premium(42) is False
    assert "Premium removed" in removed.effective_message.reply_text.await_args.args[0]

    again = make_update(user_id=1)
    await admin_module.removepremium_command(service, again, context("42"))
    assert "did not have premium" in again.effective_message.reply_text.await_args.args[0]


async def test_premium_commands_validate_input_and_owner():
    service = make_service(owner=1)
    service.bot = FakeBot()
    stranger = make_update(user_id=42)
    await admin_module.addpremium_command(service, stranger, context("42", "7"))
    assert "only for the bot owner" in stranger.effective_message.reply_text.await_args.args[0]

    missing = make_update(user_id=1)
    await admin_module.addpremium_command(service, missing, context())
    assert "Usage: /addpremium" in missing.effective_message.reply_text.await_args.args[0]

    bad_days = make_update(user_id=1)
    await admin_module.addpremium_command(service, bad_days, context("42", "0"))
    assert "1 to 3650" in bad_days.effective_message.reply_text.await_args.args[0]

    bad_remove = make_update(user_id=1)
    await admin_module.removepremium_command(service, bad_remove, context("abc"))
    assert "Usage: /removepremium" in bad_remove.effective_message.reply_text.await_args.args[0]


class FloodBot(FakeBot):
    """Raises one Telegram flood wait, then behaves normally."""

    def __init__(self):
        super().__init__()
        self.flooded = False

    async def send_message(self, chat_id=None, text="", **kwargs):
        if chat_id == 42 and not self.flooded:
            self.flooded = True
            raise RetryAfter(timedelta(seconds=3))
        return await super().send_message(chat_id=chat_id, text=text, **kwargs)


async def test_broadcast_waits_out_a_flood_error_and_still_delivers(monkeypatch):
    service = make_service(owner=1)
    service.bot = FloodBot()
    await service.store.touch_user(42, "Asha", "asha")
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(admin_module, "asyncio", SimpleNamespace(sleep=fake_sleep))
    update = make_update(user_id=1)
    await admin_module.broadcast_command(service, update, SimpleNamespace(args=["hi"], bot=service.bot))

    assert service.bot.flooded is True
    assert 3.5 in slept                                   # retry_after + a small margin
    assert any(message["chat_id"] == 42 and message["text"] == "hi" for message in service.bot.messages)
    summary = [message["text"] for message in service.bot.messages if "Broadcast finished" in message["text"]]
    assert summary and "Failed: 0" in summary[-1]
