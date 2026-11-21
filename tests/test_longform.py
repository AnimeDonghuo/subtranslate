"""Only SRTs longer than four hours are split, and their parts merged again."""

from datetime import timedelta
from pathlib import Path

import pytest
import srt

from subtranslate.large_srt import iter_file_cues, prepare_file_srt
from subtranslate.longform import (
    MAX_PART_SECONDS,
    plan_file_parts,
    plan_srt_parts,
    translate_in_parts,
)
from subtranslate.subtitles import SubtitleError, TranslatedSubtitle, prepare_subtitle


def cues(count: int, step_seconds: int) -> list[srt.Subtitle]:
    return [
        srt.Subtitle(index=index + 1,
                     start=timedelta(seconds=index * step_seconds),
                     end=timedelta(seconds=index * step_seconds + step_seconds - 1),
                     content=f"Line {index + 1}")
        for index in range(count)
    ]


def memory_source(count: int, step_seconds: int):
    data = srt.compose(cues(count, step_seconds)).encode()
    return prepare_subtitle(data, "movie.srt")


def disk_source(tmp_path: Path, count: int, step_seconds: int):
    path = tmp_path / "source.srt"
    path.write_bytes(srt.compose(cues(count, step_seconds)).encode())
    return prepare_file_srt(path)


async def test_short_srt_is_translated_in_a_single_session():
    source = memory_source(3, 5)
    seen = []

    async def translate(part):
        seen.append(part)
        return TranslatedSubtitle(part.utf8, len(part.cues), False)

    result = await translate_in_parts(translate, source, "hi")
    assert seen == [source]
    assert result.cue_count == 3


async def test_long_srt_is_split_between_cues_and_merged_with_original_timings():
    source = memory_source(6, 3600)
    parts = plan_srt_parts(source)
    assert parts is not None and len(parts) == 2
    for part in parts:
        assert (part[-1].end - part[0].start).total_seconds() <= MAX_PART_SECONDS

    seen = []

    async def translate(part):
        seen.append(len(part.cues))
        text = part.utf8.decode().replace("Line", "Hola línea")
        return TranslatedSubtitle(text.encode(), len(part.cues), False)

    result = await translate_in_parts(translate, source, "es")
    assert seen == [4, 2]
    merged = list(srt.parse(result.content.decode()))
    assert [cue.content for cue in merged] == [f"Hola línea {index}" for index in range(1, 7)]
    assert [cue.start for cue in merged] == [cue.start for cue in source.cues]
    assert [cue.end for cue in merged] == [cue.end for cue in source.cues]


async def test_a_part_with_the_wrong_number_of_cues_is_rejected():
    source = memory_source(6, 3600)

    async def translate(part):
        only_first = srt.compose(list(srt.parse(part.utf8.decode()))[:1])
        return TranslatedSubtitle(only_first.encode(), 1, False)

    with pytest.raises(SubtitleError, match="one part"):
        await translate_in_parts(translate, source, "es")


async def test_only_srt_is_ever_split():
    data = ("WEBVTT\n\n" + "".join(
        f"{index}\n00:00:01.000 --> 00:00:02.000\nHello\n\n" for index in range(1, 4))).encode()
    source = prepare_subtitle(data, "movie.vtt")
    seen = []

    async def translate(part):
        seen.append(part)
        return TranslatedSubtitle(part.utf8, len(part.cues), False)

    await translate_in_parts(translate, source, "hi")
    assert seen == [source]


async def test_disk_backed_long_srt_is_split_written_to_disk_and_merged(tmp_path):
    source = disk_source(tmp_path, 6, 3600)
    parts = plan_file_parts(source, tmp_path)
    assert parts is not None and len(parts) == 2
    assert sum(part.cue_count for part in parts) == 6
    assert all(part.path.exists() for part in parts)
    assert all((part.path.parent == tmp_path) for part in parts)

    async def translate(part):
        # The website returns a freshly translated SRT for each part.
        translated = part.path.read_text(encoding="utf-8").replace("Line", "Hola línea")
        target = tmp_path / f"download-{part.path.stem}.srt"
        target.write_text(translated, encoding="utf-8")
        return TranslatedSubtitle(target, part.cue_count, False)

    result = await translate_in_parts(translate, source, "es")
    assert isinstance(result.content, Path)
    merged = list(srt.parse(result.content.read_text(encoding="utf-8")))
    assert [cue.content for cue in merged] == [f"Hola línea {index}" for index in range(1, 7)]
    original = list(iter_file_cues(source.path, source.encoding))
    assert [cue.start for cue in merged] == [cue.start for cue in original]
    assert result.cue_count == 6


def test_short_disk_backed_srt_is_not_split(tmp_path):
    source = disk_source(tmp_path, 4, 600)
    assert plan_file_parts(source, tmp_path) is None
    assert list(tmp_path.glob("part-*.srt")) == []


async def test_disk_backed_short_srt_goes_through_unchanged(tmp_path):
    source = disk_source(tmp_path, 4, 600)
    seen = []

    async def translate(part):
        seen.append(part)
        return TranslatedSubtitle(part.path, part.cue_count, False)

    result = await translate_in_parts(translate, source, "hi")
    assert seen == [source]
    assert result.content == source.path
