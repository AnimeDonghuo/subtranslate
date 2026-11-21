"""Translate very long SRT subtitles in consecutive parts.

A single website session that has to translate many hours of dialogue can
stall. Splitting happens only for SRT files longer than four hours, always on
a cue boundary, and the parts are merged back into one file with the original
timings and the translated words untouched.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import srt

from .large_srt import FileSubtitle, _encoding, iter_file_cues, verify_file_srt
from .subtitles import (
    PreparedSubtitle,
    SubtitleError,
    TranslatedSubtitle,
    _parse,
    decode_subtitle,
    verify_and_merge,
)

MAX_PART_SECONDS = 4 * 60 * 60
# Cues are written to the merged file in small batches so a very long
# subtitle never has to fit in memory at once.
_MERGE_BATCH = 500


def plan_srt_parts(source: PreparedSubtitle, max_seconds: int = MAX_PART_SECONDS):
    """Return consecutive cue groups, or None when no split is needed.

    Cues are never cut: a part boundary only falls between two cues, so every
    timestamp stays exactly as it was in the original file.
    """
    cues = getattr(source, "cues", None)
    if source.extension != ".srt" or not cues:
        return None
    span = cues[-1].end - cues[0].start
    if span.total_seconds() <= max_seconds:
        return None
    parts: list[list[srt.Subtitle]] = []
    current: list[srt.Subtitle] = []
    part_start = cues[0].start
    for cue in cues:
        if current and (cue.end - part_start).total_seconds() > max_seconds:
            parts.append(current)
            current = []
            part_start = cue.start
        current.append(cue)
    if current:
        parts.append(current)
    return parts if len(parts) > 1 else None


def _part_source(part: list[srt.Subtitle]) -> PreparedSubtitle:
    encoded = srt.compose(part, reindex=True).encode("utf-8")
    cues = list(srt.parse(encoded.decode("utf-8")))
    return PreparedSubtitle(utf8=encoded, cues=cues, extension=".srt")


def _duration(cues) -> float | None:
    first = last = None
    for cue in cues:
        if first is None:
            first = cue.start
        last = cue.end
    return None if first is None else (last - first).total_seconds()


def plan_file_parts(source: FileSubtitle, directory: Path,
                    max_seconds: int = MAX_PART_SECONDS) -> list[FileSubtitle] | None:
    """Write one standard SRT per part of a disk-backed subtitle.

    The source file is streamed twice, so a multi-gigabyte SRT is never loaded
    as a whole. Only files longer than ``max_seconds`` are split.
    """
    duration = _duration(iter_file_cues(source.path, source.encoding))
    if duration is None or duration <= max_seconds:
        return None
    parts: list[FileSubtitle] = []
    buffer: list[srt.Subtitle] = []

    def flush() -> None:
        path = directory / f"part-{len(parts) + 1:03d}.srt"
        path.write_text(srt.compose(buffer, reindex=True), encoding="utf-8")
        parts.append(replace(source, path=path, encoding="utf-8",
                             cue_count=len(buffer), work_directory=directory))
        buffer.clear()

    part_start = None
    for cue in iter_file_cues(source.path, source.encoding):
        if part_start is None:
            part_start = cue.start
        elif buffer and (cue.end - part_start).total_seconds() > max_seconds:
            flush()
            part_start = cue.start
        buffer.append(cue)
    if buffer:
        flush()
    if len(parts) < 2:
        for part in parts:
            part.path.unlink(missing_ok=True)
        return None
    return parts


def _spill(content: bytes, directory: Path, index: int) -> Path:
    path = directory / f"download-part-{index:03d}.srt"
    path.write_bytes(content)
    return path


def merge_file_parts(source: FileSubtitle, results: list[TranslatedSubtitle], directory: Path,
                     language_code: str) -> TranslatedSubtitle:
    """Join the translated parts and re-check every timing against the original."""
    merged = directory / "merged.srt"
    index = 0
    batch: list[srt.Subtitle] = []
    with merged.open("w", encoding="utf-8", newline="") as sink:
        for number, result in enumerate(results, start=1):
            content = result.content
            path = content if isinstance(content, Path) else _spill(content, directory, number)
            for cue in iter_file_cues(path, _encoding(path)):
                index += 1
                cue.index = index
                batch.append(cue)
                if len(batch) >= _MERGE_BATCH:
                    sink.write(srt.compose(batch, reindex=False))
                    batch.clear()
        if batch:
            sink.write(srt.compose(batch, reindex=False))
    # Timings, cue counts and untranslated runs are checked again over the
    # whole merged file, not just per part.
    return verify_file_srt(source, merged, language_code)


async def _translate_memory_parts(translate, source: PreparedSubtitle,
                                  max_seconds: int, on_part) -> TranslatedSubtitle:
    parts = plan_srt_parts(source, max_seconds)
    if parts is None:
        return await translate(source)
    translated_text: list[str] = []
    for index, part in enumerate(parts, start=1):
        if on_part is not None:
            await on_part(index, len(parts))
        result = await translate(_part_source(part))
        content = result.content
        data = content.read_bytes() if isinstance(content, Path) else content
        translated = _parse(decode_subtitle(data), is_output=True)
        if len(translated) != len(part):
            raise SubtitleError("The website changed the number of subtitle cues in one part, "
                                "so the result was not sent. Please try again.")
        translated_text.extend(cue.content for cue in translated)
    merged = srt.compose([
        srt.Subtitle(index=index, start=original.start, end=original.end, content=text)
        for index, (original, text) in enumerate(zip(source.cues, translated_text), start=1)
    ])
    return verify_and_merge(source, merged.encode("utf-8"))


async def translate_in_parts(translate: Callable[[object], Awaitable[TranslatedSubtitle]],
                             source, language_code: str,
                             max_seconds: int = MAX_PART_SECONDS,
                             on_part: Callable[[int, int], Awaitable[None]] | None = None
                             ) -> TranslatedSubtitle:
    """Translate an SRT in parts and merge the translated text back.

    ``translate`` receives one part at a time. Nothing is delivered unless
    every part returns the same cues, so a partial translation can never be
    sent as if it were complete. Shorter files and other formats are passed
    through unchanged.
    """
    if isinstance(source, FileSubtitle):
        directory = source.work_directory or source.path.parent
        parts = plan_file_parts(source, directory, max_seconds)
        if parts is None:
            return await translate(source)
        results: list[TranslatedSubtitle] = []
        for index, part in enumerate(parts, start=1):
            if on_part is not None:
                await on_part(index, len(parts))
            results.append(await translate(part))
            # The part file is only needed for its own upload; dropping it keeps
            # peak disk near one part instead of the whole split copy.
            part.path.unlink(missing_ok=True)
        return await asyncio.to_thread(merge_file_parts, source, results, directory, language_code)
    return await _translate_memory_parts(translate, source, max_seconds, on_part)
