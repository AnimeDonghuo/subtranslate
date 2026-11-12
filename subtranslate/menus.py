"""User-facing menus: /start, the interactive /help and /ussetting presets.

No message in this module names the third-party translation service. Users of
the bot only ever see "the translation service" or "the website".
"""

import html
import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .config import FREE_PRESET_LIMIT
from .tasks import render_board

logger = logging.getLogger(__name__)

PRESET_PAGE_SIZE = 6
START_TEXT = (
    "🌐 <b>Advanced Subtitle Translator</b>\n\n"
    "I am an advanced translate bot: send me a subtitle in <b>one language</b> and I can "
    "translate it into <b>any other language</b> you choose.\n\n"
    "<b>How to use</b>\n"
    "1️⃣ Send a subtitle file as a document (.srt, .ass, .sub, .sbv, .vtt or .stl)\n"
    "2️⃣ Pick a language, your saved presets, or every language\n"
    "3️⃣ I translate it and send the finished file back, e.g. <code>movie.Hindi.srt</code>\n\n"
    "<b>Before you start</b>\n"
    "• Use this only for files you have permission to translate.\n"
    "• Do not translate any 18+ / adult subtitle — you will be banned.\n"
    "• Your subtitle text is sent to an online translation service, so never send "
    "confidential or private files.\n"
    "• Machine translation is not perfect; always review the result.\n\n"
    "Tap a button below to learn more."
)

SETTINGS_TEXT = (
    "⚙️ <b>Your settings</b>\n\n"
    "🌐 <b>Preset languages</b>: {summary}\n\n"
    "{limit_note}\n\n"
    "Presets appear as one ⭐ button at the top of the language picker, so your usual "
    "languages are one tap away. Select them, then tap 💾 Save."
)

PREMIUM_NOTE = (
    "⭐ <b>Premium is active</b>: unlimited presets and 🌐 Select all languages are unlocked."
)
FREE_NOTE = (
    f"Free accounts can save up to <b>{FREE_PRESET_LIMIT}</b> preset languages. "
    "Premium lifts that limit and unlocks 🌐 Select all languages — ask the owner."
)

TOPICS: dict[str, tuple[str, str]] = {
    "srt": (
        "📄 Translating a subtitle",
        ("📄 <b>Translating a subtitle</b>\n\n"
        "1. Send a subtitle file as a <b>document</b> (not as text).\n"
        "2. Tap a language, one of your ⭐ presets, or 🌐 Select all languages.\n"
        "3. Follow the stage bar. When it finishes, the translated file arrives as a new "
        "document named after the target language, e.g. <code>movie.Hindi.srt</code>.\n\n"
        "• SRT files have no bot-wide size limit; Telegram, disk space and the translation "
        "service still apply.\n"
        "• Other formats are limited to 1 MB and 3,000 cues.\n"
        "• Up to two translations run at once; extra jobs wait in the queue. When the "
        "server is short of memory the bot drops back to one at a time.\n"
        "• Use /status to see each running or queued task, and /cancel_id &lt;n&gt; to stop one.\n"
        "• Use /cancel to stop your queued jobs and active work.\n"
        "• If a job fails or is interrupted, tap the language button again."
        ),
    ),
    "formats": (
        "🗂 Supported formats",
        ("🗂 <b>Supported formats</b>\n\n"
        "✅ SRT (.srt) — full support, largest files, no cue cap\n"
        "✅ ASS / SSA (.ass) — source styles are kept\n"
        "✅ SubViewer / MicroDVD (.sub) — original timings are kept\n"
        "✅ SubViewer (.sbv)\n"
        "✅ WebVTT (.vtt)\n"
        "✅ Spruce STL (.stl) — text-based only\n\n"
        "❌ Binary VobSub and EBU STL are not supported.\n"
        "❌ Archives, images and PDFs are not subtitles."
        ),
    ),
    "lang": (
        "🌐 Languages and presets",
        ("🌐 <b>Languages and presets</b>\n\n"
        "The picker shows five languages per page with Previous/Next buttons. "
        "249 languages are available, including regional variants such as "
        "Portuguese (Portugal) and Chinese (Traditional).\n\n"
        "⭐ <b>Saved languages</b>: open /ussetting, select languages with ✅ and "
        "tap 💾 Save (free accounts: up to 10). After that, the language picker shows one extra green button, "
        "⭐ Saved languages to use — tap it and your saved set is translated one file at a "
        "time, just like pressing the language buttons.\n\n"
        "🌐 <b>Select all languages</b> is exclusive to the owner and premium users "
        "(🔒 button for everyone else); ask the owner for premium. It translates every "
        "language sequentially and can take many hours and send many files."
        ),
    ),
    "settings": (
        "⚙️ Settings",
        ("⚙️ <b>Settings</b>\n\n"
        "/ussetting — open your personal settings. It contains your ⭐ saved languages, "
        "stored per user: select several languages (they show ✅), then tap 💾 Save. Your "
        "picker then offers a ⭐ Saved languages to use button. Settings are remembered "
        "across restarts.\n\n"
        "/stats — your personal usage.\n"
        "/status — a live message with stage bars, a 🔄 Refresh button and a 🛑 Cancel "
        "button for each task; it updates itself every few seconds.\n"
        "/cancel_id &lt;n&gt; — stop one task without affecting the others.\n"
        "/leaderboard — the top users by translated subtitles."
        ),
    ),
    "privacy": (
        "🔒 Privacy and rules",
        ("🔒 <b>Privacy and rules</b>\n\n"
        "• Your subtitle text is uploaded to an online translation service. Do not send "
        "confidential, private or personal files.\n"
        "• Translate only files you own or have permission to translate.\n"
        "• No 18+ / adult subtitles. Sending them gets you banned.\n"
        "• The bot does not store your subtitle text. It keeps your Telegram ID, display "
        "name, username and a count of translated files so /stats and /leaderboard work.\n"
        "• A copy of each delivered file may be kept by the bot owner for moderation."
        ),
    ),
    "owner": (
        "🛡 Owner tools",
        ("🛡 <b>Owner tools</b>\n\n"
        "👑 <b>Access</b>\n"
        "/public — anyone may use the bot\n"
        "/private — only authorized users may use the bot\n"
        "/auth &lt;id&gt; — allow that user\n"
        "/unauth &lt;id&gt; — remove that user\n\n"
        "🚫 <b>Bans</b>\n"
        "/ban &lt;id&gt; — block a user everywhere until released\n"
        "/release &lt;id&gt; — unblock a user\n"
        "/banlist — banned users with names and usernames\n\n"
        "📊 <b>Statistics</b>\n"
        "/stats — full bot statistics\n"
        "/leaderboard — top 10 users, with today/week/month/year buttons\n\n"
        "⭐ <b>Premium</b>\n"
        "/addpremium &lt;id&gt; &lt;days&gt; — unlimited presets and 🌐 Select all languages\n"
        "/removepremium &lt;id&gt; — end premium access\n\n"
        "📣 <b>Messaging</b>\n"
        "/broadcast &lt;text&gt; — send a message to every known user (or reply to a message)\n\n"
        "Every delivered subtitle is copied to LOG_CHANNEL_ID with the sender's ID, name "
        "and username."
        ),
    ),
}

TOPIC_ORDER = ("srt", "formats", "lang", "settings", "privacy")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def _is_owner(service, user_id: int) -> bool:
    return bool(user_id) and user_id in service.settings.owner_ids


def start_keyboard(service, user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📄 How to translate", callback_data="help:srt", style="success")],
        [InlineKeyboardButton("🌐 Languages & presets", callback_data="help:lang", style="primary")],
        [InlineKeyboardButton("⚙️ My settings", callback_data="us:open")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="lb:month"),
         InlineKeyboardButton("📊 My stats", callback_data="stats:me")],
        [InlineKeyboardButton("ℹ️ Full help", callback_data="help:menu")],
    ]
    if _is_owner(service, user_id):
        rows.append([InlineKeyboardButton("👑 Owner tools", callback_data="help:owner", style="danger")])
    return InlineKeyboardMarkup(rows)


def help_keyboard(service, user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        TOPICS[key][0], callback_data=f"help:{key}",
        style="primary" if index == 0 else None,
    )] for index, key in enumerate(TOPIC_ORDER)]
    rows.append([InlineKeyboardButton("🏆 Leaderboard", callback_data="lb:month"),
                 InlineKeyboardButton("⚙️ Settings", callback_data="us:open")])
    if _is_owner(service, user_id):
        rows.append([InlineKeyboardButton("👑 Owner tools", callback_data="help:owner", style="danger")])
    return InlineKeyboardMarkup(rows)


def help_menu_text() -> str:
    return ("ℹ️ <b>Help</b>\n\n"
            "Choose a topic below. Every topic opens in this message; use /start for the "
            "quick tour.\n\n"
            "Commands: /start · /help · /ussetting · /status · /cancel_id · /stats · "
            "/leaderboard · /cancel")


def _settings_summary(names: list[str]) -> str:
    if not names:
        return "none selected yet"
    shown = ", ".join(_escape(name) for name in names[:8])
    if len(names) > 8:
        shown += f" and {len(names) - 8} more"
    return f"{len(names)} selected — {shown}"


def settings_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 Preset languages ({count})", callback_data="us:list:0",
                              style="primary")],
        [InlineKeyboardButton("💾 Save", callback_data="us:save", style="success")],
        [InlineKeyboardButton("🔙 Close", callback_data="us:close")],
    ])


def preset_list_keyboard(service, draft: set[str], page: int) -> tuple[InlineKeyboardMarkup, int, int]:
    languages = service.catalog.items()
    page_count = max(1, math.ceil(len(languages) / PRESET_PAGE_SIZE))
    page = max(0, min(page, page_count - 1))
    shown = languages[page * PRESET_PAGE_SIZE:(page + 1) * PRESET_PAGE_SIZE]
    rows = []
    for code, name in shown:
        selected = code in draft
        rows.append([InlineKeyboardButton(
            ("✅ " if selected else "▫️ ") + name,
            callback_data=f"us:t:{code}",
            style="success" if selected else "primary",
        )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹ Previous", callback_data=f"us:list:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next ›", callback_data=f"us:list:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("💾 Save", callback_data="us:save", style="success"),
                 InlineKeyboardButton("🧹 Clear all", callback_data="us:clear", style="danger")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="us:back")])
    return InlineKeyboardMarkup(rows), page + 1, page_count


def preset_list_text(service, draft: set[str], page: int, pages: int,
                     *, limit: int | None = None) -> str:
    names = [name for code, name in service.catalog.items() if code in draft]
    summary = _settings_summary(names)
    counter = f"{len(draft)}/{limit} free slots used" if limit else f"{len(draft)} selected · premium"
    note = ("Free accounts can save up to "
            f"{limit} languages; premium saves as many as you like." if limit else
            "Premium: save as many languages as you like.")
    return (f"🌐 <b>Preset languages</b> — page {page}/{pages}\n\n"
            f"{summary}\n"
            f"<i>{counter}</i>\n\n"
            "Tap a language to select or deselect it; selected languages show ✅. "
            f"Then tap 💾 Save.\n\n{note}")


def _draft_names(service, codes: set[str]) -> list[str]:
    return [name for code, name in service.catalog.items() if code in codes]


async def send_start(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await service.ensure_allowed(update):
        return
    user = update.effective_user
    await update.effective_message.reply_text(
        START_TEXT, parse_mode="HTML", reply_markup=start_keyboard(service, user.id),
        disable_web_page_preview=True,
    )


async def send_help(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await service.ensure_allowed(update):
        return
    user = update.effective_user
    await update.effective_message.reply_text(
        help_menu_text(), parse_mode="HTML", reply_markup=help_keyboard(service, user.id),
        disable_web_page_preview=True,
    )


async def _edit(query, text: str, keyboard) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML",
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        # Double taps produce an identical edit; do not log message contents.
        logger.info("Menu could not be updated (%s)", type(exc).__name__)


async def help_callback(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await service.ensure_callback_allowed(query):
        return
    topic = (query.data or "").removeprefix("help:")
    if topic == "menu":
        await query.answer()
        await _edit(query, help_menu_text(), help_keyboard(service, user.id))
        return
    if topic == "owner" and not _is_owner(service, user.id):
        await query.answer("This topic is only for the bot owner.", show_alert=True)
        return
    entry = TOPICS.get(topic)
    if entry is None:
        await query.answer("That help topic is unavailable.", show_alert=True)
        return
    await query.answer()
    rows = [[InlineKeyboardButton("🔙 Back to help", callback_data="help:menu", style="primary")]]
    await _edit(query, TOPICS[topic][1], InlineKeyboardMarkup(rows))


async def user_settings(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await service.ensure_allowed(update):
        return
    user = update.effective_user
    draft = set(await service.store.presets(user.id))
    service.preset_draft(user.id).clear()
    service.preset_draft(user.id).update(draft)
    note = PREMIUM_NOTE if await service.is_premium(user.id) else FREE_NOTE
    text = SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, draft)),
                                limit_note=note)
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=settings_keyboard(len(draft)),
    )


async def settings_callback(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await service.ensure_callback_allowed(query):
        return
    data = query.data or ""
    draft = service.preset_draft(user.id)

    premium = await service.is_premium(user.id)

    if data == "us:open":
        saved = set(await service.store.presets(user.id))
        draft.clear()
        draft.update(saved)
        await query.answer()
        note = PREMIUM_NOTE if premium else FREE_NOTE
        await _edit(query, SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, saved)),
                                                limit_note=note),
                    settings_keyboard(len(saved)))
        return

    if data.startswith("us:list:"):
        try:
            page = int(data.removeprefix("us:list:"))
        except ValueError:
            page = 0
        await query.answer()
        keyboard, current, pages = preset_list_keyboard(service, draft, page)
        await _edit(query, preset_list_text(service, draft, current, pages,
                                            limit=None if premium else FREE_PRESET_LIMIT), keyboard)
        return

    if data.startswith("us:t:"):
        code = data.removeprefix("us:t:")
        if (code not in draft and len(draft) >= FREE_PRESET_LIMIT and not premium):
            await query.answer(
                f"Free accounts can save up to {FREE_PRESET_LIMIT} preset languages. "
                "Ask the owner for premium to save more.",
                show_alert=True,
            )
            return
        if not service.catalog.get(code):
            await query.answer("That language is unavailable.", show_alert=True)
            return
        if code in draft:
            draft.discard(code)
            await query.answer("Removed")
        else:
            draft.add(code)
            await query.answer("Added ✅")
        # Stay on the page that contains the toggled language.
        languages = [value for value, _ in service.catalog.items()]
        index = languages.index(code) if code in languages else 0
        keyboard, current, pages = preset_list_keyboard(service, draft, index // PRESET_PAGE_SIZE)
        await _edit(query, preset_list_text(service, draft, current, pages,
                                            limit=None if premium else FREE_PRESET_LIMIT), keyboard)
        return

    if data == "us:clear":
        draft.clear()
        await query.answer("All presets cleared; tap Save to keep this.")
        keyboard, current, pages = preset_list_keyboard(service, draft, 0)
        await _edit(query, preset_list_text(service, draft, current, pages,
                                            limit=None if premium else FREE_PRESET_LIMIT), keyboard)
        return

    if data == "us:back":
        await query.answer()
        note = PREMIUM_NOTE if premium else FREE_NOTE
        await _edit(query, SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, draft)),
                                                limit_note=note),
                    settings_keyboard(len(draft)))
        return

    if data == "us:save":
        codes = [code for code, _name in service.catalog.items() if code in draft]
        if not premium:
            codes = codes[:FREE_PRESET_LIMIT]
        await service.store.set_presets(user.id, codes)
        service.drop_preset_draft(user.id)
        await query.answer("Saved ✅")
        await _edit(query, f"✅ <b>Saved</b>\n\n{_settings_summary(_draft_names(service, set(codes)))}.\n\n"
                           "The ⭐ Saved languages button in the language picker now uses these.",
                    settings_keyboard(len(codes)))
        return

    if data == "us:close":
        service.drop_preset_draft(user.id)
        await query.answer()
        await _edit(query, "⚙️ Settings closed. Use /ussetting to open them again.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Open settings",
                                                               callback_data="us:open",
                                                               style="primary")]]))
        return

    await query.answer("That setting is unavailable.", show_alert=True)


STATUS_EMPTY = (
    "😴 <b>No active translation</b>\n\n"
    "Send a subtitle file to start one. /status lists your running and queued tasks, "
    "and /cancel_id stops one of them."
)


def _clip(text: str, limit: int = 3900) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n…"


async def send_status(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One live, self-refreshing status message per chat.

    Asking again replaces this user's previous status message. Other users keep
    their own message and their own Refresh/Cancel buttons.
    """
    if not await service._is_private_and_allowed(update):
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    owner = service._is_owner(user.id)
    tasks = service.tasks.all() if owner else service.tasks.for_chat(chat_id)
    if not tasks:
        await update.effective_message.reply_text(STATUS_EMPTY, parse_mode="HTML")
        return
    if service.bot is not None:
        await service.status_boards.show(service, chat_id=chat_id, viewer_id=user.id)
    else:
        await update.effective_message.reply_text(
            _clip(render_board(tasks, owner=owner)), parse_mode="HTML")


async def cancel_task_work(service, *, task_id: int, user_id: int | None, owner: bool,
                           chat_id: int) -> str:
    """Shared by /cancel_id and the status message's Cancel buttons."""
    outcome, task = service.tasks.request_cancel(task_id, user_id=user_id, owner=owner)
    if outcome == "missing":
        return f"No task #{task_id} is running. Use /status to see current task numbers."
    if outcome == "forbidden":
        return f"Task #{task_id} belongs to another user. Only the bot owner can stop it."
    if task.chat_id != chat_id:
        await service._notice(task.chat_id, f"Your task #{task_id} was stopped by the bot owner.")
    if outcome == "queued":
        removed = service.queue.cancel_job(task.job_key)
        service.tasks.remove(task_id)
        return (f"🗑 Task #{task_id} removed from the queue." if removed
                else f"Task #{task_id} had already left the queue.")
    # The task's own cancellation event also aborts an in-flight download or
    # upload, not just the website stage.
    return f"🛑 Stopping task #{task_id}. The current stage will stop and no unfinished file will be sent."


async def cancel_task(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel_id <n>: stop one task for its owner, or for the owner of the bot."""
    if not await service._is_private_and_allowed(update):
        return
    message = update.effective_message
    user = update.effective_user
    raw = ((context.args or [""])[0]).strip().lstrip("#")
    if not raw.isdigit():
        await message.reply_text("Usage: /cancel_id 12 — use the task number shown by /status.")
        return
    text = await cancel_task_work(service, task_id=int(raw), user_id=user.id,
                                  owner=service._is_owner(user.id),
                                  chat_id=update.effective_chat.id)
    await message.reply_text(text)
    board = service.status_boards.get(update.effective_chat.id)
    if board is not None:
        # Show the cancellation on the live status immediately.
        await board.refresh()


async def status_callback(service, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh and per-task Cancel buttons on the live status message."""
    query = update.callback_query
    user = query.from_user
    if not await service.ensure_callback_allowed(query):
        return
    data = query.data or ""
    chat_id = update.effective_chat.id
    if data == "status:refresh":
        board = service.status_boards.get(chat_id)
        if board is None or service.bot is None:
            await query.answer("Use /status to open the live status.")
            return
        await board.refresh()
        await query.answer("Refreshed ✅")
        return
    if data.startswith("status:cancel:"):
        try:
            task_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("That task button is no longer valid.", show_alert=True)
            return
        text = await cancel_task_work(service, task_id=task_id, user_id=user.id,
                                      owner=service._is_owner(user.id), chat_id=chat_id)
        await query.answer(text[:200], show_alert=True)
        board = service.status_boards.get(chat_id)
        if board is not None:
            await board.refresh()
        return
    await query.answer("That status action is unavailable.", show_alert=True)


def install_menu_handlers(service, application) -> None:
    application.add_handler(CommandHandler(["start"], lambda update, context: send_start(
        service, update, context)))
    application.add_handler(CommandHandler(["help"], lambda update, context: send_help(
        service, update, context)))
    application.add_handler(CommandHandler("ussetting", lambda update, context: user_settings(
        service, update, context)))
    application.add_handler(CommandHandler("status", lambda update, context: send_status(
        service, update, context)))
    application.add_handler(CommandHandler("cancel_id", lambda update, context: cancel_task(
        service, update, context)))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: status_callback(service, update, context), pattern=r"^status:"))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: help_callback(service, update, context), pattern=r"^help:"))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: settings_callback(service, update, context),
        pattern=r"^us:"))
