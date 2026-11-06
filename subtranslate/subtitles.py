"""Validate supported subtitle files and preserve or verify cue timings."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

import pysubs2
import srt
from charset_normalizer import from_bytes

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 5_000_000
MAX_CUES = 3000
SUPPORTED_EXTENSIONS = frozenset({".srt", ".ass", ".sub", ".sbv", ".vtt", ".stl"})


class SubtitleError(ValueError):
    """An input or downloaded subtitle cannot safely be used."""


@dataclass(frozen=True)
class OtherCue:
    start: int | tuple[int, ...]
    end: int | tuple[int, ...]
    content: str


@dataclass(frozen=True)
class PreparedSubtitle:
    utf8: bytes
    cues: list[srt.Subtitle] | list[OtherCue]
    extension: str = ".srt"


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
            raise SubtitleError("Could not read the text encoding. Save the subtitle as UTF-8 and try again.")
        text = str(match)
    if "\x00" in text:
        raise SubtitleError("This does not look like a text subtitle. Save it as UTF-8 and try again.")
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


def subtitle_extension(filename: str) -> str:
    """Accept only the formats offered by the website, regardless of case."""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = "." + basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    if extension not in SUPPORTED_EXTENSIONS:
        raise SubtitleError("Please send an SRT, ASS, SUB, SBV, VTT, or text-based STL subtitle file.")
    return extension


_TIME = r"\d+:\d{2}:\d{2}\.\d{1,3}"
_SBV_LINE = re.compile(rf"^[ \t]*({_TIME})[ \t]*,[ \t]*({_TIME})[ \t]*\r?$", re.MULTILINE)
_MICRODVD_LINE = re.compile(r"^[ \t]*\{(\d+)\}\{(\d+)\}([^\r\n]*)", re.MULTILINE)
_STL_LINE = re.compile(
    r"^[ \t]*(\d+:\d{2}:\d{2}[:.]\d+)[ \t]*,[ \t]*(\d+:\d{2}:\d{2}[:.]\d+)[ \t]*,[ \t]*([^\r\n]+)",
    re.MULTILINE,
)


def _is_frame_rate_declaration(index: int, match: re.Match) -> bool:
    return (index == 0 and match[1] == match[2] and match[1] in {"0", "1"}
            and bool(re.fullmatch(r"\d+(?:\.\d+)?", match[3].strip())))


def _milliseconds(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    whole, fraction = seconds.split(".")
    if int(minutes) >= 60 or int(whole) >= 60:
        raise ValueError("Invalid timestamp")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(whole)) * 1000 + int(fraction.ljust(3, "0"))


def _parse_sbv(text: str) -> list[OtherCue]:
    matches = list(_SBV_LINE.finditer(text))
    cues = []
    for i, match in enumerate(matches):
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[match.end():end_pos].strip()
        cues.append(OtherCue(_milliseconds(match[1]), _milliseconds(match[2]), content))
    return cues


def _parse_other(text: str, extension: str) -> list[OtherCue]:
    try:
        if extension in {".ass", ".vtt"}:
            if extension == ".vtt" and not text.lstrip().startswith("WEBVTT"):
                raise SubtitleError("The VTT subtitle must start with WEBVTT.")
            parsed = pysubs2.SSAFile.from_string(text, format_=extension[1:])
            cues = [OtherCue(event.start, event.end, event.plaintext)
                    for event in parsed.events if not event.is_comment]
        elif extension == ".sbv":
            cues = _parse_sbv(text)
        elif extension == ".sub":
            matches = list(_MICRODVD_LINE.finditer(text))
            cues = []
            for index, match in enumerate(matches):
                # MicroDVD may begin with a frame-rate declaration, not a cue.
                if _is_frame_rate_declaration(index, match):
                    continue
                cues.append(OtherCue(int(match[1]), int(match[2]), match[3]))
            if not matches:
                # Common text-based SubViewer .sub files use SBV-like timings.
                cues = _parse_sbv(text)
        elif extension == ".stl":
            cues = [OtherCue(tuple(map(int, re.split(r"[:.]", match[1]))),
                             tuple(map(int, re.split(r"[:.]", match[2]))), match[3])
                    for match in _STL_LINE.finditer(text)]
        else:
            raise SubtitleError("Unsupported subtitle format.")
    except (ValueError, TypeError, IndexError, OverflowError) as exc:
        if isinstance(exc, SubtitleError):
            raise
        raise SubtitleError("The subtitle could not be parsed. Check its format and timestamps.") from exc
    if not cues:
        if extension == ".stl":
            raise SubtitleError("Use a text-based Spruce STL file; binary EBU STL is not supported.")
        raise SubtitleError("The subtitle has no readable cues in this format.")
    if len(cues) > MAX_CUES:
        raise SubtitleError(f"The file has more than {MAX_CUES:,} cues. Split it and try again.")
    if any(cue.end < cue.start or (isinstance(cue.start, int) and cue.start < 0) or not cue.content.strip()
           for cue in cues):
        raise SubtitleError("The subtitle contains an empty cue or an invalid timestamp.")
    if extension == ".stl" and any(
        value[1] >= 60 or value[2] >= 60 or value[3] >= 100
        for cue in cues for value in (cue.start, cue.end)
    ):
        raise SubtitleError("The subtitle contains an invalid STL timecode.")
    return cues


def prepare_subtitle(data: bytes, filename: str = "subtitle.srt") -> PreparedSubtitle:
    extension = subtitle_extension(filename)
    if len(data) > MAX_INPUT_BYTES:
        raise SubtitleError("The subtitle is too large. The limit is 1,000,000 bytes (about 1 MB).")
    text = decode_subtitle(data)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise SubtitleError("The subtitle exceeds 1 MB after conversion to UTF-8. Split it and try again.")
    cues = _parse(text) if extension == ".srt" else _parse_other(text, extension)
    return PreparedSubtitle(utf8=encoded, cues=cues, extension=extension)


def subtitle_for_website(source: PreparedSubtitle) -> PreparedSubtitle:
    """Bridge text .sub variants through SRT when the site's SUB parser drops cues.

    Synthetic times only identify cues during translation: the original .sub
    frame numbers or timestamps remain untouched in the final output.
    """
    if source.extension != ".sub":
        return source
    text = source.utf8.decode("utf-8")
    microdvd = bool(_MICRODVD_LINE.search(text))
    site_cues = []
    for index, cue in enumerate(source.cues, start=1):
        content = cue.content.replace("|", "\n") if microdvd else re.sub(r"\[br\]", "\n", cue.content, flags=re.IGNORECASE)
        start = timedelta(seconds=(index - 1) * 2)
        site_cues.append(srt.Subtitle(index=index, start=start, end=start + timedelta(seconds=1), content=content))
    encoded = srt.compose(site_cues, reindex=False).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise SubtitleError("The SUB file exceeds 1 MB when prepared for translation. Split it and try again.")
    return PreparedSubtitle(utf8=encoded, cues=site_cues, extension=".srt")


def restore_subtitle_from_website(source: PreparedSubtitle, site_result: TranslatedSubtitle) -> TranslatedSubtitle:
    """Put translated SRT cue text back into a text .sub without changing its timing."""
    if source.extension != ".sub":
        return site_result
    translated_cues = _parse(decode_subtitle(site_result.content), is_output=True)
    original = source.utf8.decode("utf-8")
    microdvd = list(_MICRODVD_LINE.finditer(original))
    spans: list[tuple[int, int, str]] = []
    if microdvd:
        for index, match in enumerate(microdvd):
            if not _is_frame_rate_declaration(index, match):
                spans.append((match.start(3), match.end(3), "|"))
    else:
        matches = list(_SBV_LINE.finditer(original))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(original)
            segment = original[start:end]
            leading = len(segment) - len(segment.lstrip())
            trailing = len(segment) - len(segment.rstrip())
            separator = "[br]" if re.search(r"\[br\]", segment, flags=re.IGNORECASE) else ("\r\n" if "\r\n" in segment else "\n")
            spans.append((start + leading, end - trailing, separator))
    if len(spans) != len(translated_cues):
        raise SubtitleError("The translated SUB cues could not be matched to the original. Please try again.")
    for (start, end, separator), cue in reversed(list(zip(spans, translated_cues))):
        replacement = cue.content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", separator)
        original = original[:start] + replacement + original[end:]
    # Independently re-check the restored frame numbers or timestamps.
    return verify_and_merge(source, original.encode("utf-8"))


def verify_and_merge(source: PreparedSubtitle, downloaded: bytes) -> TranslatedSubtitle:
    if len(downloaded) > MAX_OUTPUT_BYTES:
        raise SubtitleError("The website returned an unexpectedly large file. Please try again.")
    text = decode_subtitle(downloaded)
    translated = _parse(text, is_output=True) if source.extension == ".srt" else _parse_other(text, source.extension)
    if len(source.cues) != len(translated):
        raise SubtitleError("The website changed the number of subtitle cues, so the result was not sent. Please try again.")
    if source.extension != ".srt":
        for original, result in zip(source.cues, translated):
            if original.start != result.start or original.end != result.end:
                raise SubtitleError("The website changed subtitle timestamps, so the result was not sent. Please try again.")
        unchanged = all(a.content.strip() == b.content.strip() for a, b in zip(source.cues, translated))
        if source.extension == ".ass":
            # The site rebuilds ASS downloads with default styles. Retain the
            # source styles, dialogue metadata and comments while replacing its
            # dialogue text; inline override tags may still be changed by the site.
            original_ass = pysubs2.SSAFile.from_string(source.utf8.decode("utf-8"), format_="ass")
            result_ass = pysubs2.SSAFile.from_string(text, format_="ass")
            original_events = (event for event in original_ass.events if not event.is_comment)
            result_events = (event for event in result_ass.events if not event.is_comment)
            for original_event, result_event in zip(original_events, result_events):
                original_event.text = result_event.text
            text = original_ass.to_string(format_="ass")
        return TranslatedSubtitle(content=text.encode("utf-8"), cue_count=len(translated), unchanged=unchanged)
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
    try:
        extension = subtitle_extension(basename)
    except SubtitleError:
        extension = ".srt"
        stem = basename
    else:
        stem = basename[:-len(extension)]
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
    suffix = f".{language_slug}{extension}"
    while len((stem + suffix).encode("utf-8")) > 200 and stem:
        stem = stem[:-1]
    return f"{stem.rstrip(' .') or 'subtitles'}{suffix}"
