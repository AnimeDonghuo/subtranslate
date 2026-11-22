"""Format conversion: every offered target is produced and read back."""

import pytest

from subtranslate.convert import (
    DONE_CALLBACK,
    TARGET_CODES,
    ConvertError,
    convert_subtitle,
    converted_filename,
    format_keyboard,
    format_prompt,
)
from subtranslate.subtitles import SubtitleError

SRT = (
    b"1\n00:00:01,000 --> 00:00:02,500\nHello world\nsecond line\n\n"
    b"2\n00:00:41,250 --> 00:01:05,000\nAnother line\n\n"
)
ASS = (
    b"[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
    b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    b"Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\i1}Hello{\\i0}\\Nworld\n"
    b"Comment: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,ignored comment\n"
    b"Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\p1}m 0 0 l 10 10\n"
    b"Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,Second line\n"
)
STL = b"00:00:01:00,00:00:02:50,Hello world\n00:00:41:25,00:01:05:00,Another line\n"


@pytest.mark.parametrize("target", TARGET_CODES)
def test_every_target_is_produced_and_reads_back(target):
    output, count = convert_subtitle(SRT, "English.srt", target)

    assert count == 2
    assert output.startswith(b"\xef\xbb\xbf") is False
    # Reading our own output back must find the same two cues.
    again, count_again = convert_subtitle(output, f"converted.{target}", "srt")
    assert count_again == 2 and b"Hello world" in again and b"Another line" in again


def test_targets_have_the_expected_shape():
    assert convert_subtitle(SRT, "English.srt", "vtt")[0].startswith(b"WEBVTT")
    assert convert_subtitle(SRT, "English.srt", "sub")[0].startswith(b"{24}{60}")
    assert convert_subtitle(SRT, "English.srt", "sbv")[0].startswith(b"0:00:01.000,0:00:02.500")
    assert convert_subtitle(SRT, "English.srt", "stl")[0].startswith(b"00:00:01:00,00:00:02:50")
    assert convert_subtitle(SRT, "English.srt", "srt")[0].startswith(b"1\n00:00:01,000 --> 00:00:02,500")


def test_multiple_lines_become_one_line_where_the_format_has_no_newlines():
    microdvd, _ = convert_subtitle(SRT, "English.srt", "sub")
    assert b"Hello world|second line" in microdvd
    stl, _ = convert_subtitle(SRT, "English.srt", "stl")
    assert b"Hello world|second line" in stl


def test_stl_tuples_are_read_like_milliseconds():
    output, count = convert_subtitle(STL, "movie.stl", "srt")

    assert count == 2
    assert b"00:00:01,000 --> 00:00:02,500" in output


def test_ass_input_is_converted_without_its_comments_or_drawings():
    output, count = convert_subtitle(ASS, "bcs.ass", "srt")

    # The comment and the \p1 drawing are not subtitle text.
    assert count == 2
    assert b"ignored comment" not in output and b"m 0 0 l 10 10" not in output
    assert b"Hello\nworld" in output and b"Second line" in output


@pytest.mark.parametrize("target", ["ass", "ssa", "ASS"])
def test_ass_and_ssa_are_refused_with_the_ass_command_hint(target):
    with pytest.raises(ConvertError, match="/ass"):
        convert_subtitle(SRT, "English.srt", target)


def test_unknown_formats_are_refused():
    with pytest.raises(ConvertError, match="not supported"):
        convert_subtitle(SRT, "English.srt", "pdf")


def test_empty_and_zero_length_events_are_dropped():
    ass = (
        b"[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Kept\n"
        b"Dialogue: 0,0:00:03.00,0:00:03.00,Default,,0,0,0,,Zero length\n"
        b"Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,\n"
    )
    output, count = convert_subtitle(ass, "movie.ass", "srt")

    assert count == 1 and b"Kept" in output
    assert b"Zero length" not in output


def test_a_file_without_timed_text_is_refused():
    with pytest.raises(SubtitleError):
        convert_subtitle(b"[Script Info]\nScriptType: v4.00+\n\n[Events]\n", "empty.ass", "srt")


def test_converted_filenames_keep_the_stem_and_change_the_extension():
    assert converted_filename("English.srt", "sub") == "English.sub"
    assert converted_filename("C:\\videos\\bcs.ASS", "vtt") == "bcs.vtt"
    assert converted_filename("movie", "srt") == "movie.srt"
    assert len(converted_filename("x" * 300 + ".srt", "srt").encode()) <= 200


def test_keyboard_ticks_selection_and_has_one_done_button():
    markup = format_keyboard({"sub", "vtt"})
    flat = [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]

    assert ("✅ SUB", "fmt:sub") in flat and ("✅ VTT", "fmt:vtt") in flat
    assert ("SRT", "fmt:srt") in flat
    assert [data for _text, data in flat].count(DONE_CALLBACK) == 1
    assert format_prompt("English.srt", {"sub"}).count("SUB") >= 1
