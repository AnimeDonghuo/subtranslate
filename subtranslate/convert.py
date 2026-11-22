"""Convert a subtitle between the text formats the bot accepts.

ASS and SSA are deliberately not offered here: `/ass` already builds a styled
ASS from an SRT, and converting *to* ASS would throw away the styling a real
ASS needs, so an ASS result would look worse than the file it came from.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

import srt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .subtitles import (
    SubtitleError,
    _cue_milliseconds,
    decode_subtitle,
    prepare_subtitle,
    subtitle_extension,
)

# Button order in /changeformat. The extension is the target file extension.
TARGETS: tuple[tuple[str, str], ...] = (
    ("srt", "SRT"),
    ("sub", "SUB"),
    ("sbv", "SBV"),
    ("vtt", "VTT"),
    ("stl", "STL"),
)
TARGET_CODES = tuple(code for code, _label in TARGETS)
# ASS/SSA cannot be produced here; /ass handles them and keeps their styling.
EXCLUDED_EXTENSIONS = frozenset({"ass", "ssa"})
# Conversion happens in memory, so the source is capped like /ass.
MAX_CONVERT_BYTES = 25 * 1024 * 1024
# MicroDVD stores frame numbers, not times: a frame rate has to be assumed and
# the caption says so. 23.976 is the usual rate for the releases this bot sees.
MICRODVD_FPS = 23.976
DONE_CALLBACK = "fmt:done"
DONE_LABEL = "✅ Done"


# ASS typesetting draws shapes with commands such as "m 0 0 l 100 100". The
# scanner keeps those events, and they must not become subtitle text.
_ASS_DRAWING = re.compile(r"^\s*[mnlbsMN LBS][\s\d.,\-mnlbsMN LBS]*$")


@dataclass
class FormatDraft:
    """One /changeformat request: the file and the formats chosen so far."""

    file_id: str
    file_name: str
    file_size: int | None = None
    # The document's own message id: MTProto needs it to refetch the file.
    message_id: int = 0
    selected: set[str] = field(default_factory=set)


def _is_drawing(text: str) -> bool:
    return bool(text.strip()) and bool(_ASS_DRAWING.fullmatch(text.strip()))


class ConvertError(SubtitleError):
    """A conversion the user must fix by sending a different file."""


def _milliseconds(value) -> int:
    """Cue timings are timedeltas (SRT), ms integers or STL tuples."""
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1000)
    return _cue_milliseconds(value)


def _clock(milliseconds: int, separator: str = ".") -> str:
    """HH:MM:SS.mmm, the timestamp shape SRT and VTT share."""
    milliseconds = max(0, int(milliseconds))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _sbv_clock(milliseconds: int) -> str:
    """H:MM:SS.mmm, the timestamp shape SBV uses."""
    hours, rest = _clock(milliseconds).split(":", 1)
    return f"{int(hours)}:{rest}"


def _frame(milliseconds: int) -> int:
    return max(0, round(milliseconds * MICRODVD_FPS / 1000))


def _stl_clock(milliseconds: int) -> str:
    """HH:MM:SS:CC, the text STL (Spruce) shape."""
    milliseconds = max(0, int(milliseconds))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{millis // 10:02d}"


def _to_srt(cues: list[tuple[int, int, str]]) -> bytes:
    subtitles = [
        srt.Subtitle(index=index, start=timedelta(milliseconds=start),
                     end=timedelta(milliseconds=max(end, start + 1)), content=text)
        for index, (start, end, text) in enumerate(cues, start=1)
    ]
    return srt.compose(subtitles).encode("utf-8")


def _to_microdvd(cues: list[tuple[int, int, str]]) -> bytes:
    lines = []
    for start, end, text in cues:
        first, last = _frame(start), _frame(end)
        # MicroDVD has no sub-frame timing; an event must span at least one.
        last = max(last, first + 1)
        lines.append(f"{{{first}}}{{{last}}}{text.replace(chr(10), '|')}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _to_sbv(cues: list[tuple[int, int, str]]) -> bytes:
    blocks = [
        f"{_sbv_clock(start)},{_sbv_clock(max(end, start + 1))}\n{text}"
        for start, end, text in cues
    ]
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def _to_vtt(cues: list[tuple[int, int, str]]) -> bytes:
    blocks = ["WEBVTT"] if not cues else ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(cues, start=1):
        blocks.append(str(index))
        blocks.append(f"{_clock(start)} --> {_clock(max(end, start + 1))}")
        blocks.append(text)
        blocks.append("")
    return ("\n".join(blocks).rstrip("\n") + "\n").encode("utf-8")


def _to_stl(cues: list[tuple[int, int, str]]) -> bytes:
    lines = [
        f"{_stl_clock(start)},{_stl_clock(max(end, start + 1))},{text.replace(chr(10), '|')}"
        for start, end, text in cues
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


_WRITERS = {
    "srt": _to_srt,
    "sub": _to_microdvd,
    "sbv": _to_sbv,
    "vtt": _to_vtt,
    "stl": _to_stl,
}


def target_label(code: str) -> str:
    return dict(TARGETS).get(code, code.upper())


def check_target(code: str) -> str:
    """Validate a callback value; ASS/SSA are refused with an explanation."""
    extension = code.strip().lower().lstrip(".")
    if extension in EXCLUDED_EXTENSIONS:
        raise ConvertError("ASS/SSA is not offered here — use /ass for a styled ASS file.")
    if extension not in _WRITERS:
        raise ConvertError("That format is not supported. Choose SRT, SUB, SBV, VTT or STL.")
    return extension


def convert_subtitle(data: bytes, filename: str, target: str) -> tuple[bytes, int]:
    """Return (file bytes, cue count) for one target format."""
    extension = check_target(target)
    if not data:
        raise SubtitleError("The subtitle file is empty.")
    source = prepare_subtitle(data, filename)
    timeline: list[tuple[int, int, str]] = []
    for cue in source.cues:
        text = cue.content.replace("\r\n", "\n").replace("\r", "\n")
        # Timings come from the parsed cue, so an STL tuple works like an ms int.
        start = max(0, _milliseconds(cue.start))
        end = max(0, _milliseconds(cue.end))
        if not text.strip() or end <= start or _is_drawing(text):
            # Empty, drawing-only and zero-length ASS events carry no text.
            continue
        timeline.append((start, end, text))
    if not timeline:
        raise ConvertError("That subtitle has no timed text to convert.")
    output = _WRITERS[extension](timeline)
    _verify(output, extension, len(timeline))
    return output, len(timeline)


def _verify(output: bytes, extension: str, expected: int) -> None:
    """Parse our own output back; a conversion must not lose or break cues."""
    text = decode_subtitle(output)
    check = prepare_subtitle(output, "converted." + extension)
    if len(check.cues) != expected:
        raise ConvertError(
            f"The {extension.upper()} conversion could not be verified "
            f"({len(check.cues)} of {expected} cues), so nothing was sent. Please try another format."
        )
    if not text.strip():
        raise ConvertError(f"The {extension.upper()} conversion produced an empty file.")


def base_stem(filename: str) -> str:
    """The original name without its extension, safe to use as a file name."""
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    try:
        extension = subtitle_extension(basename)
    except SubtitleError:
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename
        extension = ""
    else:
        stem = basename[:-len(extension)]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).strip(" .")
    return stem or "subtitles"


def converted_filename(filename: str, target: str) -> str:
    extension = check_target(target)
    stem = base_stem(filename)
    # Stay well below filesystem and Telegram filename limits.
    while stem and len((stem + "." + extension).encode("utf-8")) > 200:
        stem = stem[:-1].rstrip(" .")
    return f"{stem or 'subtitles'}.{extension}"


def format_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Format buttons with a tick for each choice, then one Done button."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, label in TARGETS:
        chosen = code in selected
        row.append(InlineKeyboardButton(("✅ " if chosen else "") + label,
                                        callback_data=f"fmt:{code}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(DONE_LABEL, callback_data=DONE_CALLBACK)])
    return InlineKeyboardMarkup(rows)


def format_prompt(filename: str, selected: set[str]) -> str:
    if selected:
        chosen = ", ".join(target_label(code) for code, _label in TARGETS if code in selected)
        chosen_line = f"Selected: <b>{chosen}</b>\n\n"
    else:
        chosen_line = "Select one or more formats, then tap ✅ Done.\n\n"
    return (f"🔄 <b>Change subtitle format</b>\n\n"
            f"File: <code>{_escape(filename)}</code>\n\n"
            f"{chosen_line}"
            "ASS/SSA is not offered here — use /ass for a styled ASS file.")


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
