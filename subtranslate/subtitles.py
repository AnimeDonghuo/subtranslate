"""Validate SRT files and keep the source cue timings in the delivered file."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

import srt
from charset_normalizer import from_bytes

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 5_000_000
MAX_CUES = 3000


class SubtitleError(ValueError):
    """An input or downloaded file cannot safely be used as an SRT."""


@dataclass(frozen=True)
class PreparedSubtitle:
    utf8: bytes
    cues: list[srt.Subtitle]


@dataclass(frozen=True)
class TranslatedSubtitle:
    content: bytes
    cue_count: int
    unchanged: bool


def decode_subtitle(data: bytes) -> str:
    if not data:
        raise SubtitleError("The subtitle file is empty.")
    try:
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            text = data.decode("utf-32")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Best-effort legacy encoding detection; UTF-8 is more reliable.
        match = from_bytes(data).best()
        if match is None or match.chaos > 0.2:
            raise SubtitleError("Could not read the text encoding. Save the SRT as UTF-8 and try again.")
        text = str(match)
    if "\x00" in text:
        raise SubtitleError("This does not look like a text SRT. Save it as UTF-8 and try again.")
    return text.lstrip("\ufeff")


def _parse(text: str, *, is_output: bool = False) -> list[srt.Subtitle]:
    try:
        cues = []
        for cue in srt.parse(text):
            cues.append(cue)
            if len(cues) > MAX_CUES:
                raise SubtitleError(f"The file has more than {MAX_CUES:,} cues. Split it and try again.")
    except (srt.SRTParseError, ValueError, OverflowError) as exc:
        if isinstance(exc, SubtitleError):
            raise
        if is_output:
            raise SubtitleError("The website returned an invalid SRT. Please try again.") from exc
        raise SubtitleError("This is not a valid SRT. Check its cue numbers, timestamps and text.") from exc
    if not cues:
        raise SubtitleError("The SRT has no readable subtitle cues.")
    for cue in cues:
        if cue.start < timedelta(0) or cue.end < cue.start or not cue.content.strip():
            raise SubtitleError("The SRT contains an empty cue or an invalid timestamp.")
    return cues


def prepare_subtitle(data: bytes) -> PreparedSubtitle:
    if len(data) > MAX_INPUT_BYTES:
        raise SubtitleError("The SRT is too large. The limit is 1,000,000 bytes (about 1 MB).")
    text = decode_subtitle(data)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise SubtitleError("The SRT exceeds 1 MB after conversion to UTF-8. Split it and try again.")
    return PreparedSubtitle(utf8=encoded, cues=_parse(text))


def verify_and_merge(source: PreparedSubtitle, downloaded: bytes) -> TranslatedSubtitle:
    if len(downloaded) > MAX_OUTPUT_BYTES:
        raise SubtitleError("The website returned an unexpectedly large file. Please try again.")
    translated = _parse(decode_subtitle(downloaded), is_output=True)
    if len(source.cues) != len(translated):
        raise SubtitleError("The website changed the number of subtitle cues, so the result was not sent. Please try again.")
    merged = []
    for position, (original, result) in enumerate(zip(source.cues, translated), start=1):
        if original.start != result.start or original.end != result.end:
            raise SubtitleError("The website changed subtitle timestamps, so the result was not sent. Please try again.")
        merged.append(srt.Subtitle(
            index=original.index if original.index is not None else position,
            start=original.start,
            end=original.end,
            content=result.content,
            proprietary=original.proprietary,
        ))
    unchanged = all(a.content.strip() == b.content.strip() for a, b in zip(source.cues, translated))
    # Do not reorder/reindex: the source's timing, cue order, and indices belong
    # to the user. Only the website's translated text is substituted.
    content = srt.compose(merged, reindex=False, eol="\n").encode("utf-8")
    return TranslatedSubtitle(content=content, cue_count=len(merged), unchanged=unchanged)


def translated_filename(original: str, language: str) -> str:
    # A Telegram filename is untrusted. Never use it as a path.
    basename = original.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename[:-4] if basename.lower().endswith(".srt") else basename
    stem = "".join(
        char for char in unicodedata.normalize("NFC", stem)
        if not unicodedata.category(char).startswith("C") and char not in '<>:"/\\|?*'
    ).strip(" .") or "subtitles"
    language_slug = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFC", language), flags=re.UNICODE).strip("-")
    while len(language_slug.encode("utf-8")) > 80:
        language_slug = language_slug[:-1]
    language_slug = language_slug.rstrip("-") or "translated"
    # Stay well below common filesystem and Telegram filename limits, even
    # for names made of multi-byte characters.
    suffix = f".{language_slug}.srt"
    while len((stem + suffix).encode("utf-8")) > 200 and stem:
        stem = stem[:-1]
    return f"{stem.rstrip(' .') or 'subtitles'}{suffix}"
