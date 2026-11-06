"""Language picker matching the Google widget used by translatesubtitles.co."""

import logging
import math
import re
import string
import unicodedata
from importlib.resources import files

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
CATALOG_URL = "https://translate.google.com/translate_a/l?client=te&hl=en"
POPULAR_CODES = (
    "en", "hi", "bn", "es", "fr", "ar", "pt", "zh-CN", "ja", "ko", "de", "ru", "id", "tr", "ur", "ta",
)
PAGE_SIZE = 16
CODE_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")


def _valid_entry(code: object, name: object) -> bool:
    return (
        isinstance(code, str)
        and len(code) <= 32
        and bool(CODE_RE.fullmatch(code))
        and isinstance(name, str)
        and 0 < len(name.strip()) <= 80
        and not any(unicodedata.category(char).startswith("C") for char in name)
    )


class LanguageCatalog:
    def __init__(self) -> None:
        lines = files("subtranslate").joinpath("languages.tsv").read_text(encoding="utf-8").splitlines()
        self._languages = dict(line.split("\t", 1) for line in lines if line.strip())

    def get(self, code: str) -> str | None:
        return self._languages.get(code)

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._languages.items(), key=lambda item: item[1].casefold())

    def merge_remote(self, payload: object) -> bool:
        """Keep the bundled catalog if Google is unavailable or gives junk."""
        if not isinstance(payload, dict) or not isinstance(payload.get("tl"), dict):
            return False
        valid = {
            code: name.strip()
            for code, name in payload["tl"].items()
            if _valid_entry(code, name)
        }
        if len(valid) < 20:
            return False
        self._languages.update(valid)
        return True

    async def refresh(self) -> None:
        # This only updates the picker, not the translation backend. If this
        # undocumented endpoint changes, the bundled snapshot still works.
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(CATALOG_URL) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                self.merge_remote(data)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            logger.info("Language catalog refresh unavailable; using bundled languages")


def _rows(buttons: list[InlineKeyboardButton], width: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[pos : pos + width] for pos in range(0, len(buttons), width)]


def _language_button(code: str, name: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(name, callback_data=f"lang:{code}")


def popular_keyboard(catalog: LanguageCatalog) -> InlineKeyboardMarkup:
    buttons = [_language_button(code, catalog.get(code)) for code in POPULAR_CODES if catalog.get(code)]
    return InlineKeyboardMarkup(_rows(buttons, 2) + [[InlineKeyboardButton("🌐 All languages", callback_data="letters")]])


def _initial(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    first = normalized[:1].upper()
    return first if first in string.ascii_uppercase else "#"


def letters_keyboard(catalog: LanguageCatalog) -> InlineKeyboardMarkup:
    letters = sorted({_initial(name) for _, name in catalog.items()})
    buttons = [InlineKeyboardButton(letter, callback_data=f"group:{letter}:0") for letter in letters]
    return InlineKeyboardMarkup(_rows(buttons, 5) + [[InlineKeyboardButton("‹ Popular", callback_data="popular")]])


def group_keyboard(catalog: LanguageCatalog, letter: str, page: int) -> tuple[InlineKeyboardMarkup, int, int]:
    if letter not in string.ascii_uppercase + "#" or len(letter) != 1:
        raise ValueError("Invalid language group")
    matches = [(code, name) for code, name in catalog.items() if _initial(name) == letter]
    page_count = max(1, math.ceil(len(matches) / PAGE_SIZE))
    page = max(0, min(page, page_count - 1))
    shown = matches[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = _rows([_language_button(code, name) for code, name in shown], 2)
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹ Previous", callback_data=f"group:{letter}:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next ›", callback_data=f"group:{letter}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("‹ All languages", callback_data="letters")])
    return InlineKeyboardMarkup(rows), page + 1, page_count
