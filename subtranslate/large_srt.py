"""Validate an SRT and the website's single download with bounded memory.

The website still receives the whole subtitle once. This module does not
split, recreate, or assemble its downloaded file. Source and result remain on
job-scoped temporary storage until the actual downloaded file is sent.
"""

import codecs
import re
from contextlib import closing
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import srt
from charset_normalizer import from_bytes

from .subtitles import (
    SubtitleError,
    TranslatedSubtitle,
    UntranslatedRunDetector,
    _comparison_key,
    _parse,
)

_TIME = r"[0-9]+[,.:，．。：][0-9]+[,.:，．。：][0-9]+[,.:，．。：]?[0-9]*"
_TIMING_LINE = re.compile(rf"^[ \t]*{_TIME} *-[ -] *> *{_TIME}[^\r\n]*$")
_INDEX_LINE = re.compile(r"^[ \t]*-?[0-9]+\.?[0-9]*[ \t]*$")
_SAMPLE_BYTES = 262_144


@dataclass(frozen=True)
class FileSubtitle:
    path: Path
    encoding: str
    cue_count: int
    extension: str = ".srt"
    work_directory: Path | None = None


def _available_memory() -> int:
    """Best-effort real memory availability, accounting for container limits."""
    available = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        # In cgroup v2 the process path can be nested, rather than the root.
        path = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        current = Path("/sys/fs/cgroup") / path.lstrip("/")
        root = Path("/sys/fs/cgroup")
        while current == root or root in current.parents:
            limit = (current / "memory.max").read_text().strip()
            used = (current / "memory.current").read_text().strip()
            if limit != "max":
                available.append(max(0, int(limit) - int(used)))
            if current == root:
                break
            current = current.parent
    except (OSError, ValueError, StopIteration):
        pass
    # On other platforms use a conservative fallback, not a file-size limit.
    return min(available) if available else 256 * 1024 * 1024


def _encoding(path: Path) -> str:
    with path.open("rb") as source:
        sample = source.read(_SAMPLE_BYTES)
    if not sample:
        raise SubtitleError("The subtitle file is empty.")
    if sample.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        # A sample can end halfway through a multibyte UTF-8 character.
        codecs.getincrementaldecoder("utf-8-sig")().decode(sample, final=False)
        return "utf-8-sig"
    except UnicodeDecodeError:
        match = from_bytes(sample).best()
        if match is None or match.chaos > 0.2:
            raise SubtitleError("Could not read the text encoding. Save the subtitle as UTF-8 and try again.")
        return match.encoding


def _one_cue(lines: list[str]) -> srt.Subtitle:
    cues = _parse("".join(lines))
    if len(cues) != 1:
        raise SubtitleError("The SRT has an ambiguous cue boundary. Save it as a standard SRT and retry.")
    return cues[0]


def iter_file_cues(path: Path, encoding: str):
    """Yield one validated cue at a time, failing safely on ambiguous input."""
    block: list[str] = []
    block_chars = 0
    has_timing = False
    count = 0
    # One cue has to fit into memory for parsing and the website. Derive its
    # safety budget from the *host's current resources*, not a fixed file size
    # or cue-count ceiling. Reserve space for Python's Unicode/string copies.
    max_cue_chars = max(1, _available_memory() // 16)
    try:
        with path.open("r", encoding=encoding, newline=None) as source:
            while line := source.readline(max_cue_chars + 1):
                if "\x00" in line:
                    raise SubtitleError("This does not look like a text subtitle. Save it as UTF-8 and try again.")
                if _TIMING_LINE.fullmatch(line.rstrip("\n")):
                    if has_timing:
                        last = len(block) - 1
                        while last >= 0 and not block[last].strip():
                            last -= 1
                        index_line = ""
                        if last >= 0 and _INDEX_LINE.fullmatch(block[last].rstrip("\n")):
                            if last > 0 and block[last - 1].strip():
                                # A spoken number followed by an unnumbered cue
                                # is indistinguishable from an index here. The
                                # website might silently drop that line.
                                raise SubtitleError(
                                    "Ambiguous SRT cue boundary; add a blank line between cues and retry."
                                )
                            index_line = block.pop(last)
                        # Separating blank lines are not cue text. In
                        # particular, blanks after a cue index belong to the
                        # next header rather than the previous subtitle.
                        while block and not block[-1].strip():
                            block.pop()
                        count += 1
                        yield _one_cue(block)
                        block = [index_line] if index_line else []
                        block_chars = len(index_line)
                    has_timing = True
                block.append(line)
                block_chars += len(line)
                if block_chars > max_cue_chars:
                    raise SubtitleError(
                        "This host does not have enough available memory to process one subtitle cue safely."
                    )
        if has_timing:
            count += 1
            yield _one_cue(block)
        if not count:
            raise SubtitleError("The SRT has no readable subtitle cues.")
    except UnicodeError:
        raise SubtitleError("Could not read the whole subtitle encoding. Save it as UTF-8 and try again.") from None


def prepare_file_srt(path: Path, stop=None) -> FileSubtitle:
    """Validate the complete source before attempting the website upload."""
    encoding = _encoding(path)
    count = 0
    for _cue in iter_file_cues(path, encoding):
        if stop is not None and stop.is_set():
            raise InterruptedError("Subtitle validation canceled")
        count += 1
    return FileSubtitle(path=path, encoding=encoding, cue_count=count)


def convert_to_utf8(source: FileSubtitle, destination: Path, stop=None) -> None:
    """Convert legacy/UTF-16 input for the website without loading the file."""
    try:
        with source.path.open("r", encoding=source.encoding, newline="") as original, \
                destination.open("w", encoding="utf-8", newline="") as converted:
            while chunk := original.read(256 * 1024):
                if stop is not None and stop.is_set():
                    raise InterruptedError("Subtitle conversion canceled")
                converted.write(chunk)
    except UnicodeError:
        raise SubtitleError("Could not convert the entire subtitle to UTF-8. Please save it as UTF-8 and retry.") from None


def verify_file_srt(source: FileSubtitle, downloaded: Path, language_code: str, stop=None) -> TranslatedSubtitle:
    """Compare the complete source and single downloaded file without rewriting it."""
    output_encoding = _encoding(downloaded)
    detector = UntranslatedRunDetector(language_code)
    count = changed = run = longest = 0
    suspicious = 0
    with closing(iter_file_cues(source.path, source.encoding)) as original, \
            closing(iter_file_cues(downloaded, output_encoding)) as output:
        for before, after in zip_longest(original, output):
            if stop is not None and stop.is_set():
                raise InterruptedError("Subtitle verification canceled")
            if before is None or after is None:
                raise SubtitleError("The website download has missing or extra cues; no incomplete file was sent.")
            if before.start != after.start or before.end != after.end:
                raise SubtitleError("The website download changed subtitle timestamps; no file was sent.")
            count += 1
            if before.content.strip() != after.content.strip():
                changed += 1
            key = _comparison_key(before.content)
            if key and key == _comparison_key(after.content) and any(char.isalpha() for char in key):
                run += 1
                longest = max(longest, run)
            else:
                run = 0
            if not suspicious:
                suspicious = detector.add(before.content, after.content)
    if count != source.cue_count:
        raise SubtitleError("The website download has missing cues; no incomplete file was sent.")
    if suspicious:
        raise SubtitleError(
            "The website download appears to contain untranslated subtitles, so no incomplete file was sent. "
            "Please try again later."
        )
    return TranslatedSubtitle(downloaded, count, changed == 0, changed, longest)
