"""Incremental SRT checks; the website result is never rebuilt or combined."""

import threading

import pytest
import srt

from subtranslate.large_srt import (
    convert_to_utf8,
    iter_file_cues,
    prepare_file_srt,
    verify_file_srt,
)
from subtranslate.subtitles import SubtitleError


def srt_text(count: int, text: str = "Original subtitle") -> str:
    return "".join(
        f"{index}\n00:00:01,000 --> 00:00:02,000\n{text} {index}\n\n" for index in range(1, count + 1)
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_file_parser_matches_srt_cues_across_encodings(tmp_path, encoding):
    content = srt_text(53, "こんにちは world").replace("\n", "\r\n")
    path = tmp_path / "source.srt"
    path.write_bytes(content.encode(encoding))
    source = prepare_file_srt(path)
    assert source.cue_count == 53
    actual = list(iter_file_cues(path, source.encoding))
    expected = list(srt.parse(content))
    assert [(c.index, c.start, c.end, c.content) for c in actual] == [
        (c.index, c.start, c.end, c.content) for c in expected
    ]
    converted = tmp_path / "converted.srt"
    convert_to_utf8(source, converted)
    assert converted.read_text(encoding="utf-8") == content.replace("\r\n", "\n")


def test_more_than_previous_size_and_cue_caps_are_validated(tmp_path):
    path = tmp_path / "source.srt"
    count = 24_000
    with path.open("w", encoding="utf-8") as source:
        for index in range(1, count + 1):
            source.write(f"{index}\n00:00:01,000 --> 00:00:02,000\nOriginal subtitle cue {index}\n\n")
    assert path.stat().st_size > 1_000_000
    prepared = prepare_file_srt(path)
    assert prepared.cue_count == count
    output = tmp_path / "downloaded.srt"
    with output.open("w", encoding="utf-8") as result:
        for index in range(1, count + 1):
            result.write(f"{index + 100}\n00:00:01,000 --> 00:00:02,000\nTranslated cue {index}\n\n")
    before = output.read_bytes()
    result = verify_file_srt(prepared, output, "es")
    assert result.content == output and result.cue_count == count and result.changed_cues == count
    assert output.read_bytes() == before  # exact website file, including its numbering


def test_rejects_missing_extra_and_changed_timing(tmp_path):
    source_path = tmp_path / "source.srt"
    source_path.write_text(srt_text(3), encoding="utf-8")
    source = prepare_file_srt(source_path)
    output = tmp_path / "downloaded.srt"
    output.write_text(srt_text(2, "Translated"), encoding="utf-8")
    with pytest.raises(SubtitleError, match="missing or extra cues"):
        verify_file_srt(source, output, "es")
    output.write_text(srt_text(4, "Translated"), encoding="utf-8")
    with pytest.raises(SubtitleError, match="missing or extra cues"):
        verify_file_srt(source, output, "es")
    output.write_text(srt_text(3, "Translated").replace("00:00:02,000", "00:00:02,100", 1), encoding="utf-8")
    with pytest.raises(SubtitleError, match="timestamps"):
        verify_file_srt(source, output, "es")


def test_rejects_likely_incomplete_download_without_splitting(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="subtranslate.large_srt")
    source_path = tmp_path / "source.srt"
    raw = srt_text(40, "这是一个简单的字幕测试句子")
    source_path.write_text(raw, encoding="utf-8")
    source = prepare_file_srt(source_path)
    output = tmp_path / "downloaded.srt"
    first = raw.split("\n\n", 1)[0]
    output.write_text(raw.replace(first, first.replace("这是一个简单的字幕测试句子", "Translated first cue"), 1), encoding="utf-8")
    with pytest.raises(SubtitleError, match="untranslated subtitles"):
        verify_file_srt(source, output, "en")
    assert "changed 1 of 40 cue texts" in caplog.text and "longest unchanged run 39" in caplog.text
    assert "这是一个" not in caplog.text and "Translated first cue" not in caplog.text


def test_untranslated_run_warning_metadata_crosses_many_cues(tmp_path):
    source_path = tmp_path / "source.srt"
    raw = srt_text(40)
    source_path.write_text(raw, encoding="utf-8")
    source = prepare_file_srt(source_path)
    output = tmp_path / "downloaded.srt"
    output.write_text(raw.replace("Original subtitle 1\n", "Translated subtitle 1\n", 1), encoding="utf-8")
    result = verify_file_srt(source, output, "es")
    assert result.changed_cues == 1 and result.longest_unchanged_run == 39
    assert result.possibly_incomplete


def test_numeric_dialogue_before_an_unnumbered_cue_is_not_silently_lost(tmp_path):
    raw = "1\n00:00:01,000 --> 00:00:02,000\n42\n\n00:00:03,000 --> 00:00:04,000\nNext line\n\n"
    path = tmp_path / "source.srt"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SubtitleError, match="Ambiguous SRT cue boundary"):
        prepare_file_srt(path)


def test_blank_between_index_and_timestamp_remains_a_cue_index(tmp_path):
    raw = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n\n00:00:03,000 --> 00:00:04,000\nNext\n\n"
    path = tmp_path / "source.srt"
    path.write_text(raw, encoding="utf-8")
    source = prepare_file_srt(path)
    cues = list(iter_file_cues(path, source.encoding))
    assert [(cue.index, cue.content) for cue in cues] == [(1, "Hello"), (2, "Next")]


def test_ambiguous_boundary_is_refused_instead_of_losing_spoken_text(tmp_path):
    path = tmp_path / "ambiguous.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n42\n00:00:03,000 --> 00:00:04,000\nNext\n\n", encoding="utf-8")
    with pytest.raises(SubtitleError, match="Ambiguous SRT cue boundary"):
        prepare_file_srt(path)


def test_validation_scan_supports_cooperative_cancellation(tmp_path):
    path = tmp_path / "source.srt"
    path.write_text(srt_text(5), encoding="utf-8")
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError, match="canceled"):
        prepare_file_srt(path, stop)


def test_file_parser_fails_clearly_when_one_cue_exceeds_available_memory(tmp_path, monkeypatch):
    from subtranslate import large_srt

    path = tmp_path / "source.srt"
    path.write_text(srt_text(1, "a" * 200), encoding="utf-8")
    monkeypatch.setattr(large_srt, "_available_memory", lambda: 1024)
    with pytest.raises(SubtitleError, match="not have enough available memory"):
        prepare_file_srt(path)
