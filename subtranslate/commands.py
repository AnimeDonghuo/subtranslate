"""The bot's command menu, and the owner-only /sync and /resync commands."""

import logging

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from .admin import _require_owner

logger = logging.getLogger(__name__)

USER_COMMANDS = (
    BotCommand("start", "Start the bot and read the rules"),
    BotCommand("help", "Show every command"),
    BotCommand("ussetting", "Choose your languages and presets"),
    BotCommand("ass", "Convert an SRT into the bot's ASS style"),
    BotCommand("changeformat", "Convert a subtitle to SUB, SBV, VTT, STL or SRT"),
    BotCommand("status", "Live status of running translations"),
    BotCommand("cancel", "Cancel your queued batch"),
    BotCommand("cancel_id", "Cancel a task by its id"),
    BotCommand("id", "Show a user or chat id"),
)

OWNER_COMMANDS = USER_COMMANDS + (
    BotCommand("authlist", "List authorized users"),
    BotCommand("auth", "Authorize a user"),
    BotCommand("unauth", "Remove an authorized user"),
    BotCommand("ban", "Ban a user"),
    BotCommand("release", "Remove a ban"),
    BotCommand("banlist", "List banned users"),
    BotCommand("stats", "Bot statistics"),
    BotCommand("leaderboard", "Top users"),
    BotCommand("public", "Allow access for everyone"),
    BotCommand("private", "Restrict the bot to authorized users"),
    BotCommand("addpremium", "Give a user premium"),
    BotCommand("removepremium", "Remove premium"),
    BotCommand("broadcast", "Send a message to every user"),
    BotCommand("sync", "Re-register the command menu"),
)


async def sync_commands(service, bot) -> None:
    """Publish the command menu so newly added commands appear in Telegram."""
    await bot.set_my_commands(list(USER_COMMANDS))
    for owner_id in sorted(service.settings.owner_ids):
        try:
            # Owners also see the admin commands in their own private chat.
            await bot.set_my_commands(list(OWNER_COMMANDS),
                                      scope=BotCommandScopeChat(chat_id=owner_id))
        except TelegramError as exc:
            logger.warning("Could not set the owner command menu (%s)", type(exc).__name__)


async def sync_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    try:
        await sync_commands(service, context.bot)
    except TelegramError as exc:
        await update.effective_message.reply_text(
            f"Telegram refused the command list ({type(exc).__name__}). Please try again.")
        return
    owners = (f" and {len(OWNER_COMMANDS)} to the owner's private chat"
              if service.settings.owner_ids else "")
    await update.effective_message.reply_text(
        f"✅ Command menu synced: {len(USER_COMMANDS)} commands for everyone{owners}.")


def install_sync_handlers(service, application) -> None:
    for name in ("sync", "resync"):
        application.add_handler(CommandHandler(name, lambda update, context: sync_command(
            service, update, context)))
