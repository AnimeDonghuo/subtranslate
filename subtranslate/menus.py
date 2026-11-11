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
    "Presets appear at the top of the language picker, so your usual languages are one "
    "tap away. Select as many as you like, then tap 💾 Save."
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
        "• One translation runs at a time, with up to two queued.\n"
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
        "⭐ <b>Saved languages</b>: open /ussetting, select any number of languages with ✅ and "
        "tap 💾 Save. After that, the language picker shows one extra green button, "
        "⭐ Saved languages to use — tap it and your saved set is translated one file at a "
        "time, just like pressing the language buttons.\n\n"
        "🌐 <b>Select all languages</b> translates every language sequentially. This can "
        "take many hours and send many files."
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
            "Commands: /start · /help · /ussetting · /stats · /leaderboard · /cancel")


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


def preset_list_text(service, draft: set[str], page: int, pages: int) -> str:
    names = [name for code, name in service.catalog.items() if code in draft]
    summary = _settings_summary(names)
    return (f"🌐 <b>Preset languages</b> — page {page}/{pages}\n\n"
            f"{summary}\n\n"
            "Tap a language to select or deselect it; selected languages show ✅. "
            "Then tap 💾 Save.")


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
    text = SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, draft)))
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

    if data == "us:open":
        saved = set(await service.store.presets(user.id))
        draft.clear()
        draft.update(saved)
        await query.answer()
        await _edit(query, SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, saved))),
                    settings_keyboard(len(saved)))
        return

    if data.startswith("us:list:"):
        try:
            page = int(data.removeprefix("us:list:"))
        except ValueError:
            page = 0
        await query.answer()
        keyboard, current, pages = preset_list_keyboard(service, draft, page)
        await _edit(query, preset_list_text(service, draft, current, pages), keyboard)
        return

    if data.startswith("us:t:"):
        code = data.removeprefix("us:t:")
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
        await _edit(query, preset_list_text(service, draft, current, pages), keyboard)
        return

    if data == "us:clear":
        draft.clear()
        await query.answer("All presets cleared; tap Save to keep this.")
        keyboard, current, pages = preset_list_keyboard(service, draft, 0)
        await _edit(query, preset_list_text(service, draft, current, pages), keyboard)
        return

    if data == "us:back":
        await query.answer()
        await _edit(query, SETTINGS_TEXT.format(summary=_settings_summary(_draft_names(service, draft))),
                    settings_keyboard(len(draft)))
        return

    if data == "us:save":
        codes = [code for code, _name in service.catalog.items() if code in draft]
        await service.store.set_presets(user.id, codes)
        service.drop_preset_draft(user.id)
        await query.answer("Saved ✅")
        await _edit(query, f"✅ <b>Saved</b>\n\n{_settings_summary(_draft_names(service, set(codes)))}.\n\n"
                           "These languages now appear at the top of your language picker.",
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


def install_menu_handlers(service, application) -> None:
    application.add_handler(CommandHandler(["start"], lambda update, context: send_start(
        service, update, context)))
    application.add_handler(CommandHandler(["help"], lambda update, context: send_help(
        service, update, context)))
    application.add_handler(CommandHandler("ussetting", lambda update, context: user_settings(
        service, update, context)))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: help_callback(service, update, context), pattern=r"^help:"))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: settings_callback(service, update, context),
        pattern=r"^us:"))
