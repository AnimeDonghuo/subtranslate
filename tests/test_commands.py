"""The owner-only /sync and /resync re-register the Telegram command menu."""

from types import SimpleNamespace

from support import FakeBot, make_service, make_update

from subtranslate import commands
from subtranslate.commands import sync_command


def context_with(bot):
    return SimpleNamespace(bot=bot)


async def test_owner_sync_publishes_user_and_owner_commands():
    service = make_service(owner=1)
    bot = FakeBot()
    update = make_update(user_id=1)

    await sync_command(service, update, context_with(bot))

    assert len(bot.commands) == 2
    public, public_scope = bot.commands[0]
    assert public_scope is None
    assert {command.command for command in public} >= {
        "start", "help", "ussetting", "ass", "status", "cancel_id"}
    owner_only, owner_scope = bot.commands[1]
    assert owner_scope.chat_id == 1
    assert {command.command for command in owner_only} >= {
        "sync", "authlist", "broadcast", "addpremium"}
    assert "Command menu synced" in update.effective_message.reply_text.await_args.args[0]


async def test_sync_is_owner_only_and_asks_for_owner_id_when_unset():
    service = make_service(owner=1)
    bot = FakeBot()
    stranger = make_update(user_id=42)

    await sync_command(service, stranger, context_with(bot))
    assert "only for the bot owner" in stranger.effective_message.reply_text.await_args.args[0]
    assert bot.commands == []

    unset = make_service(owner=None)
    update = make_update(user_id=42)
    await sync_command(unset, update, context_with(bot))
    assert "OWNER_ID" in update.effective_message.reply_text.await_args.args[0]
    assert bot.commands == []


def test_sync_and_resync_are_both_registered():
    service = make_service(owner=1)
    handlers = []
    application = SimpleNamespace(add_handler=handlers.append)

    commands.install_sync_handlers(service, application)

    assert [handler.commands for handler in handlers] == [
        frozenset({"sync"}), frozenset({"resync"})]
