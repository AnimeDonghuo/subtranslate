"""All commands are registered, and specific callbacks run before the job one."""

from support import make_service
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

EXPECTED_COMMANDS = {
    "start", "help", "id", "cancel", "proxychk", "ussetting", "stats", "leaderboard",
    "ban", "release", "banlist", "auth", "unauth", "public", "private",
}
SPECIFIC_CALLBACKS = ("help:srt", "us:open", "lb:day", "stats:me")


def test_every_command_is_registered_exactly_once():
    service = make_service(owner=1)
    application = Application.builder().token("12345:TESTTOKEN").build()
    service.install_handlers(application)
    handlers = application.handlers[0]
    commands = [command for handler in handlers if isinstance(handler, CommandHandler)
                for command in handler.commands]
    assert EXPECTED_COMMANDS.issubset(set(commands))
    assert len(commands) == len(set(commands))


def test_specific_callbacks_are_registered_before_the_job_callback():
    service = make_service(owner=1)
    application = Application.builder().token("12345:TESTTOKEN").build()
    service.install_handlers(application)
    handlers = application.handlers[0]
    callbacks = [handler for handler in handlers if isinstance(handler, CallbackQueryHandler)]
    # The catch-all job handler (no pattern) must come last, otherwise it would
    # answer menu buttons with "Please choose a language again".
    assert callbacks[-1].pattern is None
    patterns = [handler.pattern for handler in callbacks[:-1]]
    for data in SPECIFIC_CALLBACKS:
        assert sum(1 for pattern in patterns if pattern and pattern.match(data)) == 1
    for data in ("page:2", "lang:hi", "all:confirm:0", "all:start", "preset:start"):
        assert not any(pattern and pattern.match(data) for pattern in patterns)
