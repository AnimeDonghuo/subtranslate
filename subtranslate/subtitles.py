"""Validate supported subtitle files and preserve or verify cue timings."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pysubs2
import srt
from charset_normalizer import from_bytes

# No bot-wide size or cue cap: SRT is streamed from disk, other text formats are
# bounded by the host's free memory and by Telegram's own ~2 GB file limit.
MAX_OUTPUT_BYTES = 2_000_000_000
SUPPORTED_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".sbv", ".vtt", ".stl"})
# Formats the translation site does not handle reliably are bridged through
# SRT. ASS, SBV, VTT and STL are uploaded in their own format, as before.
SITE_BRIDGED = frozenset({".ssa", ".sub"})


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
    # Large SRT results live in a private temporary file until Telegram has
    # finished sending them; individual website downloads remain bytes.
    content: bytes | Path
    cue_count: int
    unchanged: bool
    changed_cues: int | None = None
    longest_unchanged_run: int = 0

    @property
    def possibly_incomplete(self) -> bool:
        # Same-language sections and names can legitimately be unchanged.
        # This is a warning, not proof of a failed translation.
        return not self.unchanged and self.longest_unchanged_run >= 20


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
        cues = list(srt.parse(text))
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
        hint = (" Image-based subtitles (.sup or binary VobSub .sub/.idx) cannot be "
                "translated without OCR; extract a text SRT first.")
        raise SubtitleError("Please send an SRT, ASS, SSA, SUB, SBV, VTT, or text-based "
                            "STL subtitle file." + hint)
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
        if extension in {".ass", ".ssa", ".vtt"}:
            if extension == ".vtt" and not text.lstrip().startswith("WEBVTT"):
                raise SubtitleError("The VTT subtitle must start with WEBVTT.")
            parsed = pysubs2.SSAFile.from_string(text, format_=extension[1:])
            if not parsed.events:
                raise SubtitleError("The subtitle has no readable cues in this format.")
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
        if extension == ".sub":
            raise SubtitleError("This .sub has no text cues. A binary VobSub .sub stores images, "
                                "so it cannot be translated without OCR; extract a text SRT first.")
        raise SubtitleError("The subtitle has no readable cues in this format.")
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
    # SRT is streamed from disk for big files by the bot; other text formats are
    # parsed in memory, and the caller checks the host's free memory first.
    if extension == ".sub" and b"\x00" in data[:8192]:
        raise SubtitleError("This .sub is not a text file: a binary VobSub .sub stores images, "
                            "so it cannot be translated without OCR. Extract a text SRT first.")
    text = decode_subtitle(data)
    encoded = text.encode("utf-8")
    cues = _parse(text) if extension == ".srt" else _parse_other(text, extension)
    return PreparedSubtitle(utf8=encoded, cues=cues, extension=extension)


def _cue_milliseconds(value: int | tuple[int, ...]) -> int:
    """Turn a millisecond count, or an STL (h, m, s, centiseconds) tuple, into ms."""
    if isinstance(value, int):
        return value
    hours, minutes, seconds, centiseconds = (list(value) + [0, 0, 0, 0])[:4]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + centiseconds * 10


def _bridge_text(content: str, extension: str) -> str:
    """Make cue text safe to send as plain SRT lines."""
    if extension == ".sub":
        # MicroDVD uses |, SubViewer uses [br]; both become real SRT lines.
        return re.sub(r"\[br\]|\|", "\n", content, flags=re.IGNORECASE)
    if extension in {".sbv", ".stl"}:
        return re.sub(r"\[br\]|\|", "\n", content, flags=re.IGNORECASE)
    return content


def subtitle_for_website(source: PreparedSubtitle) -> PreparedSubtitle:
    """Send formats the site parses unreliably as an equivalent SRT.

    Cue text and real timings are preserved so the translated SRT can be
    spliced back into the untouched original file. Only MicroDVD .sub needs
    synthetic times, because its frame numbers are not real timestamps.
    """
    if source.extension not in SITE_BRIDGED:
        return source
    synthetic = source.extension == ".sub" and bool(_MICRODVD_LINE.search(source.utf8.decode("utf-8")))
    site_cues = []
    for index, cue in enumerate(source.cues, start=1):
        if synthetic:
            start = timedelta(seconds=(index - 1) * 2)
            end = start + timedelta(seconds=1)
        else:
            start = timedelta(milliseconds=_cue_milliseconds(cue.start))
            end = timedelta(milliseconds=_cue_milliseconds(cue.end))
        site_cues.append(srt.Subtitle(index=index, start=start, end=end,
                                      content=_bridge_text(cue.content, source.extension)))
    return PreparedSubtitle(utf8=srt.compose(site_cues, reindex=False).encode("utf-8"),
                            cues=site_cues, extension=".srt")


_CJK_TARGETS = frozenset({"zh-CN", "zh-TW", "yue", "ja", "ko"})
_NON_LATIN_TARGETS = frozenset({
    "zh-CN", "zh-TW", "yue", "ja", "ko", "ar", "fa", "fa-AF", "ur", "ps", "sd", "ug", "ckb",
    "hi", "ne", "mr", "sa", "bho", "mai", "awa", "doi", "gom", "bn", "as", "pa", "gu", "ta",
    "te", "kn", "ml", "si", "th", "lo", "km", "my", "el", "iw", "yi", "ru", "uk", "be", "bg",
    "mk", "sr", "kk", "ky", "mn", "tg", "ka", "hy", "am", "ti",
})


def _plain_cue_text(text: str) -> str:
    return re.sub(r"<[^>]*>|\{\\[^}]*\}", "", text)


def _comparison_key(text: str) -> str:
    """Ignore markup, spacing and punctuation when looking for likely copies."""
    normalized = unicodedata.normalize("NFKC", _plain_cue_text(text)).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _cjk_letter_count(text: str) -> int:
    return sum(
        0x3400 <= ord(char) <= 0x9FFF or 0xF900 <= ord(char) <= 0xFAFF
        or 0x3040 <= ord(char) <= 0x30FF or 0xAC00 <= ord(char) <= 0xD7AF
        for char in text
    )


class UntranslatedRunDetector:
    """Incremental, conservative script-mismatch signal; not a language detector."""

    def __init__(self, language_code: str):
        self.language_code = language_code
        self.run = self.chars = self.gap = 0
        self.distinct: set[int] = set()

    def add(self, original: str, output: str) -> int:
        before, after = original.strip(), output.strip()
        key = _comparison_key(before)
        if not key or key != _comparison_key(after):
            self.run = self.chars = self.gap = 0
            self.distinct.clear()
            return 0
        clean = _plain_cue_text(before)
        cjk = _cjk_letter_count(clean) if self.language_code not in _CJK_TARGETS else 0
        latin = (sum("LATIN" in unicodedata.name(char, "") and char.isalpha() for char in clean)
                 if self.language_code in _NON_LATIN_TARGETS else 0)
        weight = cjk if cjk >= 3 else latin if latin >= 12 else 0
        if weight:
            self.run += 1
            self.chars += weight
            # Fingerprints avoid retaining potentially huge cue text in memory.
            self.distinct.add(hash(key))
            self.gap = 0
            if self.run >= 5 and self.chars >= 30 and len(self.distinct) >= 3:
                return self.run
        else:
            self.gap += 1
            if self.gap > 2:
                self.run = self.chars = self.gap = 0
                self.distinct.clear()
        return 0


def obviously_untranslated_run(source: PreparedSubtitle, result: TranslatedSubtitle, language_code: str) -> int:
    """Find a strong script-mismatch signal, not a general language detector.

    Unknown and mixed scripts can evade this conservative check; translation
    quality cannot be guaranteed. Names and same-language files may be unchanged.
    """
    text = decode_subtitle(result.content)
    translated = _parse(text, is_output=True) if source.extension == ".srt" else _parse_other(text, source.extension)
    detector = UntranslatedRunDetector(language_code)
    for original, output in zip(source.cues, translated):
        run = detector.add(original.content, output.content)
        if run:
            return run
    return 0


def _block_spans(original: str, pattern: re.Pattern) -> list[tuple[int, int, str]]:
    """Text spans after each timing line, plus the separator used for line breaks."""
    matches = list(pattern.finditer(original))
    spans = []
    for index, match in enumerate(matches):
        start = match.end()
        limit = matches[index + 1].start() if index + 1 < len(matches) else len(original)
        segment = original[start:limit]
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment) - len(segment.rstrip())
        separator = "[br]" if re.search(r"\[br\]", segment, flags=re.IGNORECASE) else (
            "\r\n" if "\r\n" in segment else "\n")
        spans.append((start + leading, start + len(segment) - trailing, separator))
    return spans


def _text_spans(original: str, extension: str) -> list[tuple[int, int, str]]:
    """Character spans of every cue's text in the original .sub file."""
    if extension != ".sub":
        raise SubtitleError("Unsupported subtitle format.")
    if _MICRODVD_LINE.search(original):
        matches = list(_MICRODVD_LINE.finditer(original))
        return [(match.start(3), match.end(3), "|") for index, match in enumerate(matches)
                if not _is_frame_rate_declaration(index, match)]
    # Text-based SubViewer files use SBV-like timings.
    return _block_spans(original, _SBV_LINE)


def restore_subtitle_from_website(source: PreparedSubtitle, site_result: TranslatedSubtitle) -> TranslatedSubtitle:
    """Put translated cue text back into the original file, leaving everything else untouched."""
    if source.extension not in SITE_BRIDGED:
        # The site returns these formats itself: SRT as downloaded, ASS merged
        # onto its own styling, and SBV/VTT/STL uploaded and returned as-is.
        return site_result
    translated_cues = _parse(decode_subtitle(site_result.content), is_output=True)
    if source.extension == ".ssa":
        original_ssa = pysubs2.SSAFile.from_string(source.utf8.decode("utf-8"), format_="ssa")
        events = [event for event in original_ssa.events if not event.is_comment]
        if len(events) != len(translated_cues):
            raise SubtitleError("The translated SSA cues could not be matched to the original. Please try again.")
        for event, cue in zip(events, translated_cues):
            event.text = cue.content.replace("\r\n", "\n").replace("\n", "\\N")
        restored = original_ssa.to_string(format_="ssa").encode("utf-8")
        return verify_and_merge(source, restored)
    original = source.utf8.decode("utf-8")
    spans = _text_spans(original, source.extension)
    if len(spans) != len(translated_cues):
        raise SubtitleError(f"The translated {source.extension[1:].upper()} cues could not be matched "
                            "to the original. Please try again.")
    for (start, end, separator), cue in reversed(list(zip(spans, translated_cues))):
        replacement = cue.content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", separator)
        original = original[:start] + replacement + original[end:]
    # Independently re-check the restored timings and cue text.
    return verify_and_merge(source, original.encode("utf-8"))


def _download_as_utf8(downloaded: bytes, text: str) -> bytes:
    """Keep the site's bytes when already UTF-8; convert other encodings safely."""
    try:
        decoded = downloaded.decode("utf-8")
        if "\x00" not in decoded:
            return downloaded
    except UnicodeDecodeError:
        pass
    return text.encode("utf-8")


def verify_and_merge(source: PreparedSubtitle, downloaded: bytes) -> TranslatedSubtitle:
    """Validate the actual download; merge only formats needing metadata restoration."""
    if len(downloaded) > MAX_OUTPUT_BYTES:
        raise SubtitleError("The website returned an unexpectedly large file. Please try again.")
    text = decode_subtitle(downloaded)
    translated = _parse(text, is_output=True) if source.extension == ".srt" else _parse_other(text, source.extension)
    if len(source.cues) != len(translated):
        raise SubtitleError("The website changed the number of subtitle cues, so the result was not sent. Please try again.")
    for original, result in zip(source.cues, translated):
        if original.start != result.start or original.end != result.end:
            raise SubtitleError("The website changed subtitle timestamps, so the result was not sent. Please try again.")
    changed_cues = 0
    unchanged_run = longest_unchanged_run = 0
    for original, result in zip(source.cues, translated):
        if original.content.strip() != result.content.strip():
            changed_cues += 1
        key = _comparison_key(original.content)
        if key and key == _comparison_key(result.content) and any(char.isalpha() for char in key):
            unchanged_run += 1
            longest_unchanged_run = max(longest_unchanged_run, unchanged_run)
        else:
            unchanged_run = 0
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
        content = original_ass.to_string(format_="ass").encode("utf-8")
    else:
        # Send the website's actual Download file, not its on-page preview.
        # Keep byte-for-byte output when it is UTF-8.
        content = _download_as_utf8(downloaded, text)
    return TranslatedSubtitle(content=content, cue_count=len(translated), unchanged=changed_cues == 0,
                              changed_cues=changed_cues, longest_unchanged_run=longest_unchanged_run)


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
