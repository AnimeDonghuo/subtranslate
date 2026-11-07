import pytest
import srt

from subtranslate.subtitles import (
    MAX_CUES,
    MAX_INPUT_BYTES,
    SubtitleError,
    prepare_subtitle,
    restore_subtitle_from_website,
    subtitle_for_website,
    translated_filename,
    verify_and_merge,
)

SOURCE = """10\r
00:00:01,200 --> 00:00:02,300 X1:10\r
こんにちは、世界！\r
\r
25\r
00:00:03,000 --> 00:00:05,500\r
مرحبا بالعالم\r
\r
"""
TRANSLATED = """1
00:00:01,200 --> 00:00:02,300
Hello, world!

2
00:00:03,000 --> 00:00:05,500
Welcome!

"""


def test_unicode_and_utf16_input():
    for raw in (SOURCE.encode("utf-8-sig"), SOURCE.encode("utf-16")):
        prepared = prepare_subtitle(raw)
        assert prepared.utf8.decode("utf-8").startswith("10\r\n")
        assert len(prepared.cues) == 2
        assert prepared.cues[1].content == "مرحبا بالعالم"


def test_srt_sends_the_actual_utf8_download_without_rewriting_it():
    prepared = prepare_subtitle(SOURCE.encode())
    # The site can re-number cues, use CRLF, or include a UTF-8 BOM. Keep its
    # downloaded bytes rather than re-creating an SRT from the page preview.
    downloaded = b"\xef\xbb\xbf" + TRANSLATED.replace("\n", "\r\n").encode()
    result = verify_and_merge(prepared, downloaded)
    assert result.content == downloaded
    cues = list(srt.parse(result.content.decode("utf-8-sig")))
    assert [c.index for c in cues] == [1, 2]
    assert [(c.start, c.end) for c in cues] == [(c.start, c.end) for c in prepared.cues]
    assert [c.content for c in cues] == ["Hello, world!", "Welcome!"]
    assert result.cue_count == 2 and result.changed_cues == 2 and not result.unchanged


def test_reports_changed_cue_count_without_claiming_unchanged_names_failed():
    source = prepare_subtitle(b"1\n00:00:01,000 --> 00:00:02,000\nAlice\n\n"
                              b"2\n00:00:03,000 --> 00:00:04,000\nHello\n\n")
    downloaded = source.utf8.replace(b"Hello", b"Hola")
    result = verify_and_merge(source, downloaded)
    assert result.cue_count == 2 and result.changed_cues == 1 and not result.unchanged
    assert result.content == downloaded


def test_srt_converts_non_utf8_download_to_utf8_after_validation():
    prepared = prepare_subtitle(SOURCE.encode())
    result = verify_and_merge(prepared, TRANSLATED.encode("utf-16"))
    assert result.content == TRANSLATED.encode("utf-8")
    assert result.cue_count == 2


def test_unchanged_output_is_reported_not_silently_claimed_translated():
    prepared = prepare_subtitle(SOURCE.encode())
    result = verify_and_merge(prepared, SOURCE.encode())
    assert result.unchanged


def test_rejects_changed_timing_or_dropped_cues():
    prepared = prepare_subtitle(SOURCE.encode())
    with pytest.raises(SubtitleError, match="timestamps"):
        verify_and_merge(prepared, TRANSLATED.replace("00:00:02,300", "00:00:02,301").encode())
    with pytest.raises(SubtitleError, match="number of subtitle cues"):
        verify_and_merge(prepared, TRANSLATED.split("\n\n2\n")[0].encode())


def test_reject_invalid_and_oversized_files():
    for data in [b"", b"Not a subtitle file", b"\x00\x01\x00\x01"]:
        with pytest.raises(SubtitleError):
            prepare_subtitle(data)
    with pytest.raises(SubtitleError, match="1 MB"):
        prepare_subtitle(b"x" * (MAX_INPUT_BYTES + 1), "movie.vtt")


def test_srt_has_no_3000_cue_limit():
    many = "".join(f"{i}\n00:00:01,000 --> 00:00:02,000\nHi\n\n" for i in range(1, MAX_CUES + 2))
    assert len(prepare_subtitle(many.encode()).cues) == MAX_CUES + 1


def test_safe_and_informative_output_names():
    assert translated_filename("../../movie.SRT", "Hindi") == "movie.Hindi.srt"
    assert translated_filename(r"C:\\foo\\film.srt", "Chinese (Simplified)") == "film.Chinese-Simplified.srt"
    assert translated_filename("映画.srt", "Portuguese (Brazil)") == "映画.Portuguese-Brazil.srt"
    result = translated_filename("../" + "界" * 200 + ".srt", "日本語")
    assert len(result.encode()) <= 200
    assert result.endswith(".日本語.srt")
    assert "/" not in result


@pytest.mark.parametrize("encoding,text", [
    ("cp1251", "Привет, мир! Как твои дела? Это хороший фильм."),
    ("cp1256", "مرحبا بالعالم كيف الحال"),
    ("shift_jis", "こんにちは、世界。さようなら。"),
    ("windows-1252", "Bonjour, où êtes-vous ? Je suis très heureux."),
])
def test_common_legacy_encodings_are_converted_when_detectable(encoding, text):
    data = f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n\n".encode(encoding)
    assert text in prepare_subtitle(data).utf8.decode("utf-8")


@pytest.mark.parametrize("filename,source,changed_time", [
    ("film.vtt", "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n\n", "00:00:02.100"),
    ("film.sbv", "0:00:01.000,0:00:02.000\nHello\n\n", "0:00:02.100"),
    ("film.sub", "{0}{0}25.000\n{25}{50}Hello\n", "{51}"),
    ("film.stl", "// Spruce STL\n00:00:01:00,00:00:02:00,Hello\n", "00:00:02:01"),
    ("film.ass", ("[Script Info]\nScriptType: v4.00+\n[Events]\n"
     "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
     "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n"), "0:00:02.10"),
])
def test_other_supported_text_formats_keep_extension_and_verify_timing(filename, source, changed_time):
    prepared = prepare_subtitle(source.encode(), filename)
    assert len(prepared.cues) == 1
    assert prepared.extension == "." + filename.rsplit(".", 1)[1]
    output = source.replace("Hello", "Hola").encode()
    result = verify_and_merge(prepared, output)
    assert not result.unchanged and result.cue_count == 1 and b"Hola" in result.content
    assert translated_filename(filename, "Spanish") == filename.replace(".", ".Spanish.")
    old_end = {
        "vtt": "00:00:02.000", "sbv": "0:00:02.000", "sub": "{50}",
        "stl": "00:00:02:00", "ass": "0:00:02.00",
    }[filename.rsplit(".", 1)[1]]
    with pytest.raises(SubtitleError, match="timestamps"):
        verify_and_merge(prepared, source.replace(old_end, changed_time).encode())


@pytest.mark.parametrize("with_fps", [False, True])
def test_microdvd_sub_is_bridged_through_srt_and_restored_with_original_frames(with_fps):
    header = "{1}{1}23.976\r\n" if with_fps else ""
    source_text = header + "{25}{75}Hello|world\r\n{100}{150}Thank you!\r\n"
    source = prepare_subtitle(source_text.encode(), "movie.sub")
    site_source = subtitle_for_website(source)
    assert site_source.extension == ".srt"
    assert len(site_source.cues) == 2
    assert b"Hello\nworld" in site_source.utf8
    translated = site_source.utf8.replace(b"Hello\nworld", b"Hola\nmundo").replace(b"Thank you!", b"Gracias!")
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, translated))
    assert restored.content.decode() == header + "{25}{75}Hola|mundo\r\n{100}{150}Gracias!\r\n"
    assert not restored.unchanged and restored.cue_count == 2


def test_subviewer_sub_is_bridged_and_retains_header_timestamps_and_line_breaks():
    source_text = (
        "[INFORMATION]\r\n[TITLE]Original\r\n[END INFORMATION]\r\n[SUBTITLE]\r\n"
        "00:00:01.00,00:00:03.00\r\nHello[br]world\r\n\r\n"
        "00:00:04.00,00:00:06.00\r\nThank you!\r\n"
    )
    source = prepare_subtitle(source_text.encode(), "movie.sub")
    site_source = subtitle_for_website(source)
    assert len(site_source.cues) == 2 and b"Hello\nworld" in site_source.utf8
    translated = site_source.utf8.replace(b"Hello\nworld", b"Hola\nmundo").replace(b"Thank you!", b"Gracias!")
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, translated))
    assert restored.content.decode() == source_text.replace("Hello[br]world", "Hola[br]mundo").replace("Thank you!", "Gracias!")
    assert not restored.unchanged and restored.cue_count == 2


def test_ass_preserves_source_styles_dialogue_metadata_and_comments():
    import pysubs2

    source = (
        "[Script Info]\nTitle: Original\nScriptType: v4.00+\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Custom,OriginalFont,24,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Comment: 0,0:00:00.00,0:00:01.00,Custom,,0,0,0,,Original note\n"
        "Dialogue: 2,0:00:01.00,0:00:02.00,Custom,Speaker,12,13,14,,Hello\n"
    )
    # Mimic the site's rebuilding of the ASS file with a default style.
    downloaded = (
        "[Script Info]\nTitle: subtitles\nScriptType: v4.00+\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hola\n"
    )
    result = verify_and_merge(prepare_subtitle(source.encode(), "movie.ass"), downloaded.encode())
    merged = pysubs2.SSAFile.from_string(result.content.decode(), format_="ass")
    assert merged.info["Title"] == "Original"
    assert merged.styles["Custom"].fontname == "OriginalFont"
    assert merged.events[0].is_comment and merged.events[0].text == "Original note"
    dialogue = merged.events[1]
    assert dialogue.style == "Custom" and dialogue.name == "Speaker" and dialogue.layer == 2
    assert (dialogue.marginl, dialogue.marginr, dialogue.marginv) == (12, 13, 14)
    assert dialogue.plaintext == "Hola"


def test_rejects_unknown_and_binary_subtitle_formats():
    with pytest.raises(SubtitleError, match="SRT, ASS"):
        prepare_subtitle(b"hello", "movie.txt")
    with pytest.raises(SubtitleError):
        prepare_subtitle(b"\x00\x01\x00\x01", "movie.stl")


def _many_srt(count: int, text: str = "这是一个简单的字幕测试句子") -> bytes:
    return "".join(
        f"{i + 10}\n00:00:01,000 --> 00:00:02,000\n{text} {i}\n\n" for i in range(count)
    ).encode("utf-8")


def test_long_unchanged_sections_are_warnings_not_assumed_failures():
    source = prepare_subtitle(_many_srt(40, "Already in the target language"))
    partial = source.utf8.replace(b"Already in the target language 0", b"Translated first cue")
    result = verify_and_merge(source, partial)
    assert result.changed_cues == 1
    assert result.longest_unchanged_run == 39
    assert result.possibly_incomplete
    unchanged = verify_and_merge(source, source.utf8)
    assert unchanged.unchanged and not unchanged.possibly_incomplete


def test_obvious_wrong_script_run_is_detected_without_rejecting_names_or_same_language():
    from subtranslate.subtitles import obviously_untranslated_run

    source = prepare_subtitle(_many_srt(25))
    partial = source.utf8.replace("这是一个简单的字幕测试句子 0".encode(), b"Translated first cue")
    result = verify_and_merge(source, partial)
    assert obviously_untranslated_run(source, result, "en") >= 5
    assert obviously_untranslated_run(source, result, "zh-CN") == 0
    assert obviously_untranslated_run(source, result, "ja") == 0
    short_names = prepare_subtitle(_many_srt(25, "张三"))
    assert obviously_untranslated_run(short_names, verify_and_merge(short_names, short_names.utf8), "en") == 0
    already_english = prepare_subtitle(_many_srt(25, "A line already in English"))
    assert obviously_untranslated_run(already_english, verify_and_merge(already_english, already_english.utf8), "en") == 0
    assert obviously_untranslated_run(already_english, verify_and_merge(already_english, already_english.utf8), "hi") >= 5
    # Whitespace or punctuation changes must not hide untranslated Chinese.
    reformatted = source.utf8.replace("字幕测试句子".encode(), "字幕测试句子，".encode())
    reformatted_result = verify_and_merge(source, reformatted)
    assert obviously_untranslated_run(source, reformatted_result, "en") >= 5
    assert reformatted_result.possibly_incomplete
    # Repetition of one name is not, by itself, proof of a failed translation.
    repeated = prepare_subtitle("".join(
        f"{i}\n00:00:01,000 --> 00:00:02,000\n很长的重复名字\n\n" for i in range(1, 26)
    ).encode())
    assert obviously_untranslated_run(repeated, verify_and_merge(repeated, repeated.utf8), "en") == 0
