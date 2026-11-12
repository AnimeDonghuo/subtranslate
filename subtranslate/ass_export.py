"""Convert an SRT into a styled ASS subtitle that is easy to hardsub.

The default look follows the sample: bold white Arial with a thick black
outline and a soft shadow, centred near the bottom but raised so subtitles
already burned into the video (for example Chinese lines) stay visible below.

ASS cannot blur the video behind text. The nearest option is an opaque plate
with transparency, which ``/ass box`` produces instead of an outline.
"""

import re
import unicodedata
from datetime import timedelta

from .subtitles import SubtitleError, _parse, decode_subtitle

# Conversion happens in memory, so keep a generous but bounded ceiling.
MAX_ASS_SOURCE_BYTES = 25 * 1024 * 1024
MAX_ASS_CUES = 100_000

STYLE_NAME = "Sigma"
FONT_NAME = "Arial"
FONT_SIZE = 54
MARGIN_V = 120
MARGIN_LR = 80
PLAY_RES_X = 1920
PLAY_RES_Y = 1080

_BREAK_TAG = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^<>]{1,64}>")
# Only real ASS override blocks ({\...}) are removed; literal {text} is kept
# and escaped, so dialogue is never silently dropped.
_ASS_BLOCK = re.compile(r"\{\\[^{}]{0,512}\}")
_EMPTY_LINES = re.compile(r"\n{2,}")

_HEADER = f"""[Script Info]
; Converted by the bot. Hardsub example:
;   ffmpeg -i input.mkv -vf "ass=subtitle.ass" -c:a copy output.mkv
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""

_EVENTS = """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _style_line(*, plate: bool) -> str:
    if plate:
        # BorderStyle 3 draws a translucent box behind each text line. This is
        # the closest ASS can get to a blurred backdrop; it cannot blur video.
        border, outline, shadow, back = 3, 2, 0, "&H78000000"
    else:
        border, outline, shadow, back = 1, 3, 1.5, "&H80000000"
    return (
        f"Style: {STYLE_NAME},{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H000000FF,"
        f"&H00000000,{back},-1,0,0,0,100,100,0.6,0,{border},{outline},{shadow},2,"
        f"{MARGIN_LR},{MARGIN_LR},{MARGIN_V},1"
    )


def _ass_time(value: timedelta) -> str:
    total = round(max(0.0, value.total_seconds()) * 100)  # centiseconds
    hours, rest = divmod(total, 3600 * 100)
    minutes, rest = divmod(rest, 60 * 100)
    seconds, centiseconds = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _escape(text: str) -> str:
    """Plain text for an ASS Dialogue line: no markup, literal braces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BREAK_TAG.sub("\n", text)
    text = _HTML_TAG.sub("", text)        # <i>, <b>, <font ...> from SRT
    text = _ASS_BLOCK.sub("", text)       # stray {\...} override blocks
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = _EMPTY_LINES.sub("\n", text)
    # libass prints \{ and \} literally; unescaped braces would start a tag.
    return text.replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def srt_to_ass(data: bytes, plate: bool = False) -> tuple[bytes, int]:
    """Return (ASS bytes, cue count) for an SRT file."""
    if len(data) > MAX_ASS_SOURCE_BYTES:
        raise SubtitleError("This SRT is too large to convert; keep it under 25 MB.")
    text = decode_subtitle(data)
    cues = _parse(text)
    if len(cues) > MAX_ASS_CUES:
        raise SubtitleError(f"This SRT has more than {MAX_ASS_CUES:,} cues; split it and try again.")
    dialogues = []
    for cue in cues:
        content = _escape(cue.content)
        if not content:
            continue
        dialogues.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},{STYLE_NAME},,0,0,0,,{content}"
        )
    if not dialogues:
        raise SubtitleError("This SRT has no usable subtitle text.")
    document = _HEADER + _style_line(plate=plate) + _EVENTS + "\r\n".join(dialogues) + "\r\n"
    return document.encode("utf-8"), len(dialogues)


def ass_filename(original: str) -> str:
    """A safe <name>.ass that never contains a path or control characters."""
    basename = original.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    stem = "".join(
        char for char in unicodedata.normalize("NFC", stem)
        if not unicodedata.category(char).startswith("C") and char not in '<>:"/\\|?*'
    ).strip(" .") or "subtitles"
    while len((stem + ".ass").encode("utf-8")) > 200 and stem:
        stem = stem[:-1]
    return f"{stem.rstrip(' .') or 'subtitles'}.ass"
