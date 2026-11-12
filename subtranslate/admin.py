"""Owner commands: access mode, authorization, bans, statistics and ranking."""

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .config import FREE_PRESET_LIMIT, PREMIUM_DEFAULT_DAYS
from .store import PERIODS

logger = logging.getLogger(__name__)
PERIOD_LABELS = {"day": "Today", "week": "This week", "month": "This month", "year": "This year"}
MEDALS = ("🥇", "🥈", "🥉")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def _person(user_id: int, name: str = "", username: str = "") -> str:
    label = _escape(name) if name else "Unknown name"
    if username:
        label += f" (@{_escape(username)})"
    return f"{label} · <code>{user_id}</code>"


def _parse_id(args) -> int | None:
    if not args or len(args) != 1:
        return None
    try:
        value = int(args[0].strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _is_owner(service, user) -> bool:
    return bool(user) and user.id in service.settings.owner_ids


async def _require_owner(service, update: Update) -> bool:
    if not service.settings.owner_ids:
        await update.effective_message.reply_text(
            "👑 Set OWNER_ID in the deployment before using owner commands."
        )
        return False
    if not _is_owner(service, update.effective_user):
        await update.effective_message.reply_text("👑 This command is only for the bot owner.")
        return False
    return True


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def render_stats(stats, *, extra_authorized: int = 0) -> str:
    mode = "🌍 Public — anyone may use the bot" if stats.mode == "public" else (
        "🔒 Private — only authorized users may use the bot")
    if stats.backend != "mongodb":
        storage = "💾 MongoDB storage: not connected (in-memory settings)"
    elif stats.storage_total:
        free = max(0, (stats.storage_total or 0) - (stats.storage_used or 0))
        storage = (f"💾 MongoDB storage: {_human_bytes(stats.storage_used)} used · "
                   f"{_human_bytes(free)} free of {_human_bytes(stats.storage_total)}")
    else:
        storage = f"💾 MongoDB storage: {_human_bytes(stats.storage_used)} used · quota unknown"
    backend = "MongoDB" if stats.backend == "mongodb" else "in-memory (MongoDB not connected)"
    return (
        "📊 <b>Bot statistics</b>\n\n"
        f"👥 Users: <b>{stats.users_total:,}</b> total · {stats.users_active_7d:,} active (7d)\n"
        f"🚫 Banned: <b>{stats.banned:,}</b>\n"
        f"🔐 Authorized: <b>{stats.authorized + extra_authorized:,}</b>\n"
        f"⚙️ Mode: {mode}\n\n"
        f"📄 Subtitles translated: <b>{stats.translations_total:,}</b>\n"
        f"   • today: {stats.translations_day:,}\n"
        f"   • this week: {stats.translations_week:,}\n"
        f"   • this month: {stats.translations_month:,}\n"
        f"   • this year: {stats.translations_year:,}\n\n"
        f"{storage}\n"
        f"🗄 Backend: {backend}"
    )


def render_user_stats(counts: dict[str, int], rank: int | None, global_total: int) -> str:
    rank_line = (f"🏆 Your rank this month: <b>#{rank}</b>\n" if rank else
                 "🏆 No translations this month yet\n")
    return (
        "📊 <b>Your statistics</b>\n\n"
        f"📄 Subtitles translated: <b>{counts.get('total', 0):,}</b>\n"
        f"   • today: {counts.get('day', 0):,} · this week: {counts.get('week', 0):,} "
        f"· this month: {counts.get('month', 0):,}\n"
        f"{rank_line}"
        f"🌐 All users together: {global_total:,} subtitles\n\n"
        "Use /leaderboard to see the top users."
    )


def leaderboard_keyboard(period: str) -> InlineKeyboardMarkup:
    labels = {"day": "📅 Today", "week": "🗓 Week", "month": "📆 Month", "year": "🎯 Year"}
    row = []
    for key in PERIODS:
        active = key == period
        row.append(InlineKeyboardButton(
            ("✅ " if active else "") + labels[key],
            callback_data=f"lb:{key}",
            style="success" if active else None,
        ))
    return InlineKeyboardMarkup([row])


async def render_leaderboard(service, period: str, user_id: int) -> str:
    entries = await service.store.leaderboard(period, limit=10)
    lines = [f"🏆 <b>Leaderboard — {PERIOD_LABELS[period]}</b>\n"]
    if not entries:
        lines.append("No subtitles have been translated in this period yet.")
    else:
        for index, entry in enumerate(entries, start=1):
            badge = MEDALS[index - 1] if index <= len(MEDALS) else f"{index}."
            lines.append(f"{badge} {_person(entry.user_id, entry.name, entry.username)} · "
                         f"{entry.count:,} srt")
    rank, mine = await service.store.rank(period, user_id)
    if rank:
        lines.append(f"\n📍 Your rank: <b>#{rank}</b> · {mine:,} srt")
    else:
        lines.append("\n📍 You have not translated anything in this period yet.")
    return "\n".join(lines)


async def stats_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await service.ensure_allowed(update):
        return
    owner = _is_owner(service, update.effective_user)
    if owner:
        text = render_stats(await service.store.stats(),
                            extra_authorized=len(service.settings.allowed_user_ids or ()))
    else:
        user_id = update.effective_user.id
        counts = await service.store.user_counts(user_id)
        rank, _mine = await service.store.rank("month", user_id)
        total = (await service.store.stats()).translations_total
        text = render_user_stats(counts, rank, total)
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              disable_web_page_preview=True)


async def stats_callback(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await service.ensure_callback_allowed(query):
        return
    await query.answer()
    if _is_owner(service, user):
        text = render_stats(await service.store.stats(),
                            extra_authorized=len(service.settings.allowed_user_ids or ()))
    else:
        counts = await service.store.user_counts(user.id)
        rank, _mine = await service.store.rank("month", user.id)
        total = (await service.store.stats()).translations_total
        text = render_user_stats(counts, rank, total)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("🏆 Leaderboard", callback_data="lb:month",
                                                               style="primary")
                                      ]]),
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        logger.info("Statistics could not be updated (%s)", type(exc).__name__)


async def leaderboard_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await service.ensure_allowed(update):
        return
    period = "month"
    args = getattr(context, "args", None) or []
    if args and args[0].lower() in PERIODS:
        period = args[0].lower()
    text = await render_leaderboard(service, period, update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=leaderboard_keyboard(period),
                                              disable_web_page_preview=True)


async def leaderboard_callback(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await service.ensure_callback_allowed(query):
        return
    period = (query.data or "").removeprefix("lb:")
    if period not in PERIODS:
        await query.answer("That period is unavailable.", show_alert=True)
        return
    await query.answer()
    text = await render_leaderboard(service, period, query.from_user.id)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML",
                                      reply_markup=leaderboard_keyboard(period),
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        logger.info("Leaderboard could not be updated (%s)", type(exc).__name__)


async def _announce_ban(service, user_id: int) -> None:
    if service.bot is None:
        return
    try:
        await service.bot.send_message(
            chat_id=user_id,
            text="🚫 You have been banned from using this bot. Contact the owner if you "
                 "believe this is a mistake.",
        )
    except TelegramError as exc:
        logger.info("Could not notify the banned user (%s)", type(exc).__name__)


async def ban_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    target = _parse_id(getattr(context, "args", None))
    if target is None:
        await update.effective_message.reply_text("Usage: /ban <telegram user id>")
        return
    if target in service.settings.owner_ids:
        await update.effective_message.reply_text("👑 The bot owner cannot be banned.")
        return
    if target == update.effective_user.id:
        await update.effective_message.reply_text("You cannot ban yourself.")
        return
    entry = await service.store.ban(target, update.effective_user.id)
    # Stop work that was already queued or running for that user.
    removed, was_active = service.cancel_user_work(target)
    await _announce_ban(service, target)
    stopped = ""
    if removed or was_active:
        stopped = f"\nStopped {removed} queued job(s)" + (" and their active job." if was_active else ".")
    await update.effective_message.reply_text(
        f"🚫 Banned {_person(target, entry.name, entry.username)}.\n"
        f"They cannot use the bot until /release {target}.{stopped}"
    )


async def release_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    target = _parse_id(getattr(context, "args", None))
    if target is None:
        await update.effective_message.reply_text("Usage: /release <telegram user id>")
        return
    if not service.store.is_banned(target):
        await update.effective_message.reply_text(f"User {target} is not banned.")
        return
    entry = service.store.ban_info(target)
    await service.store.release(target)
    name = _person(target, entry.name if entry else "", entry.username if entry else "")
    await update.effective_message.reply_text(f"✅ Released {name}. They can use the bot again.")


async def banlist_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    entries = service.store.banned
    if not entries:
        await update.effective_message.reply_text("✅ The ban list is empty.")
        return
    lines = [f"🚫 <b>Banned users ({len(entries)})</b>\n"]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. {_person(entry.user_id, entry.name, entry.username)}")
        if entry.banned_at:
            lines.append(f"   since {entry.banned_at:%Y-%m-%d}")
    lines.append("\nUse /release <id> to unban someone.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML",
                                              disable_web_page_preview=True)


async def auth_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    target = _parse_id(getattr(context, "args", None))
    if target is None:
        await update.effective_message.reply_text("Usage: /auth <telegram user id>")
        return
    if target in service.settings.owner_ids:
        await update.effective_message.reply_text("👑 Owners are always authorized.")
        return
    if service.store.is_authorized(target):
        await update.effective_message.reply_text(f"User {target} is already authorized.")
        return
    await service.store.authorize(target)
    info = await service.store.user_info(target) or {}
    await update.effective_message.reply_text(
        f"✅ Authorized {_person(target, info.get('name', ''), info.get('username', ''))}.\n"
        "They can use the bot while it is private."
    )


def _named(info: dict[int, dict], user_id: int) -> tuple[str, str]:
    entry = info.get(user_id) or {}
    return entry.get("name", "") or "", entry.get("username", "") or ""


AUTHLIST_PAGE = 40


async def authlist_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everyone who can use the bot: owners, env allowlist and /auth entries."""
    if not await _require_owner(service, update):
        return
    owners = sorted(service.settings.owner_ids)
    env_ids = sorted(service.settings.allowed_user_ids or ())
    authorized = sorted(service.store.authorized)
    info = await service.store.users_info(sorted(set(owners) | set(env_ids) | set(authorized)))

    lines = ["🔐 <b>Access list</b>", ""]
    if owners:
        lines.append(f"👑 <b>Owners</b> · {len(owners)} (always allowed)")
        lines.extend(f"• {_person(uid, *_named(info, uid))}" for uid in owners)
        lines.append("")
    if env_ids:
        lines.append(f"🌍 <b>ALLOWED_USER_IDS</b> · {len(env_ids)}")
        lines.extend(f"• {_person(uid, *_named(info, uid))}" for uid in env_ids)
        lines.append("")
    lines.append(f"✅ <b>Added with /auth</b> · {len(authorized)}")
    if authorized:
        lines.extend(f"• {_person(uid, *_named(info, uid))}" for uid in authorized[:AUTHLIST_PAGE])
        if len(authorized) > AUTHLIST_PAGE:
            lines.append(f"… and {len(authorized) - AUTHLIST_PAGE} more")
    else:
        lines.append("• none yet — add one with <code>/auth &lt;id&gt;</code>")
    mode = service.store.mode
    lines.append("")
    lines.append(f"🚪 <b>Mode</b>: {mode} — " + (
        "anyone in a private chat may use the bot, except banned users."
        if mode == "public"
        else "only owners, ALLOWED_USER_IDS and /auth entries may use the bot."
    ))
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900].rstrip() + "\n\n… list truncated"
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def unauth_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    target = _parse_id(getattr(context, "args", None))
    if target is None:
        await update.effective_message.reply_text("Usage: /unauth <telegram user id>")
        return
    if target in service.settings.owner_ids:
        await update.effective_message.reply_text("👑 Owners are always authorized.")
        return
    if service.settings.allowed_user_ids and target in service.settings.allowed_user_ids:
        await update.effective_message.reply_text(
            f"User {target} is authorized by ALLOWED_USER_IDS in the deployment. "
            "Remove that ID from the environment to revoke access."
        )
        return
    if not service.store.is_authorized(target):
        await update.effective_message.reply_text(f"User {target} is not authorized.")
        return
    await service.store.unauthorize(target)
    info = await service.store.user_info(target) or {}
    await update.effective_message.reply_text(
        f"🚫 Removed authorization for {_person(target, info.get('name', ''), info.get('username', ''))}."
    )


async def public_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    await service.store.set_mode("public")
    await update.effective_message.reply_text(
        "🌍 <b>Public mode enabled.</b>\n\nAnyone in a private chat can use the bot, except "
        "banned users. Use /private to allow only authorized users.",
        parse_mode="HTML",
    )


async def private_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    await service.store.set_mode("private")
    await update.effective_message.reply_text(
        "🔒 <b>Private mode enabled.</b>\n\nOnly owners, IDs in ALLOWED_USER_IDS and users "
        "added with /auth can use the bot. Add people with /auth <id>.",
        parse_mode="HTML",
    )


def _retry_after_seconds(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    return float(value.total_seconds()) if hasattr(value, "total_seconds") else float(value)


async def _send_with_flood_wait(bot, *, chat_id: int, text: str) -> None:
    """Send one message; a Telegram flood wait is slept through, not dropped."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except RetryAfter as exc:
        delay = min(60.0, _retry_after_seconds(exc) + 0.5)
        logger.info("Telegram flood wait: pausing the broadcast for %.0f seconds", delay)
        await asyncio.sleep(delay)
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)


async def broadcast_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message every known user once, skipping bans and reporting the outcome."""
    if not await _require_owner(service, update):
        return
    message = update.effective_message
    text = " ".join(context.args or []).strip()
    reply = getattr(message, "reply_to_message", None)
    if not text and reply is not None:
        text = (getattr(reply, "text", "") or getattr(reply, "caption", "") or "").strip()
    if not text:
        await message.reply_text(
            "Usage: /broadcast <text> — or reply to a message with /broadcast.\n"
            "Everyone the bot has seen gets it, except banned users."
        )
        return
    if len(text) > 4000:
        await message.reply_text("Keep a broadcast under 4000 characters.")
        return
    targets = sorted(set(await service.store.all_user_ids()) | set(service.settings.owner_ids)
                     | set(service.store.authorized))
    if not targets:
        await message.reply_text("There are no known users yet, so there is nobody to message.")
        return
    notice = await message.reply_text(f"📣 Broadcasting to {len(targets)} user(s)…")
    chat_id = getattr(notice, "chat_id", None) or message.chat.id
    sent = failed = skipped = 0
    for number, user_id in enumerate(targets, start=1):
        if service.store.is_banned(user_id):
            skipped += 1
            continue
        try:
            await _send_with_flood_wait(context.bot, chat_id=user_id, text=text)
            sent += 1
        except TelegramError as exc:
            # Blocked bots and deleted accounts are expected; never log the text.
            logger.info("Broadcast delivery failed (%s)", type(exc).__name__)
            failed += 1
        # Stay well below Telegram's roughly 30 messages per second.
        await asyncio.sleep(0.05)
        if number % 25 == 0:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=notice.message_id,
                                                    text=f"📣 Broadcasting… {number}/{len(targets)}")
            except TelegramError:
                pass
    summary = f"📣 <b>Broadcast finished</b>\n\n✅ Sent: {sent}\n⚠️ Failed: {failed}"
    if skipped:
        summary += f"\n🚫 Skipped (banned): {skipped}"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=notice.message_id,
                                            text=summary, parse_mode="HTML")
    except TelegramError:
        await message.reply_text(summary, parse_mode="HTML")


async def _notify_premium(service, user_id: int, text: str) -> None:
    """Tell the affected user; a blocked bot must not fail the command."""
    try:
        await service.bot.send_message(chat_id=user_id, text=text)
    except TelegramError as exc:
        logger.info("Premium notice failed (%s)", type(exc).__name__)


async def addpremium_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addpremium <id> <days>: unlimited presets and select-all for that user."""
    if not await _require_owner(service, update):
        return
    args = context.args or []
    try:
        target = int(str(args[0]).strip()) if args else 0
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        await update.effective_message.reply_text(
            "Usage: /addpremium <user id> <days>\nExample: /addpremium 123456789 30\n"
            "Premium unlocks unlimited preset languages and 🌐 Select all languages. "
            "Give a user's numeric ID, which /id shows."
        )
        return
    days = PREMIUM_DEFAULT_DAYS
    if len(args) > 1:
        try:
            days = int(args[1])
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 3650:
            await update.effective_message.reply_text("Days must be a whole number from 1 to 3650.")
            return
    until = await service.store.grant_premium(target, days)
    info = await service.store.user_info(target) or {}
    await update.effective_message.reply_text(
        f"⭐ <b>Premium enabled</b>\n\n{_person(target, info.get('name', ''), info.get('username', ''))}\n"
        f"Expires: <code>{until:%Y-%m-%d %H:%M} UTC</code> ({days} day(s))\n\n"
        "Unlimited presets and 🌐 Select all languages are now unlocked.",
        parse_mode="HTML",
    )
    await _notify_premium(
        service, target,
        f"⭐ Premium enabled by the bot owner: unlimited preset languages and 🌐 Select all "
        f"languages, until {until:%Y-%m-%d %H:%M} UTC.",
    )


async def removepremium_command(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(service, update):
        return
    target = _parse_id(context.args or [])
    if target is None:
        await update.effective_message.reply_text("Usage: /removepremium <user id>")
        return
    had_premium = await service.store.revoke_premium(target)
    info = await service.store.user_info(target) or {}
    person = _person(target, info.get('name', ''), info.get('username', ''))
    await update.effective_message.reply_text(
        f"⭐ Premium removed for {person}." if had_premium
        else f"{person} did not have premium."
    )
    if had_premium:
        await _notify_premium(
            service, target,
            "⭐ Your premium access has ended. You can still save up to "
            f"{FREE_PRESET_LIMIT} preset languages; 🌐 Select all languages is locked again.",
        )


def install_admin_handlers(service, application) -> None:
    application.add_handler(CommandHandler("stats", lambda update, context: stats_command(
        service, update, context)))
    application.add_handler(CommandHandler("leaderboard", lambda update, context: leaderboard_command(
        service, update, context)))
    application.add_handler(CommandHandler("ban", lambda update, context: ban_command(
        service, update, context)))
    application.add_handler(CommandHandler("release", lambda update, context: release_command(
        service, update, context)))
    application.add_handler(CommandHandler("banlist", lambda update, context: banlist_command(
        service, update, context)))
    application.add_handler(CommandHandler("auth", lambda update, context: auth_command(
        service, update, context)))
    application.add_handler(CommandHandler("unauth", lambda update, context: unauth_command(
        service, update, context)))
    application.add_handler(CommandHandler("authlist", lambda update, context: authlist_command(
        service, update, context)))
    application.add_handler(CommandHandler("public", lambda update, context: public_command(
        service, update, context)))
    application.add_handler(CommandHandler("private", lambda update, context: private_command(
        service, update, context)))
    application.add_handler(CommandHandler("broadcast", lambda update, context: broadcast_command(
        service, update, context)))
    application.add_handler(CommandHandler("addpremium", lambda update, context: addpremium_command(
        service, update, context)))
    application.add_handler(CommandHandler("removepremium", lambda update, context: removepremium_command(
        service, update, context)))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: leaderboard_callback(service, update, context), pattern=r"^lb:"))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: stats_callback(service, update, context), pattern=r"^stats:"))
