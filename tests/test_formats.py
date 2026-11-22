"""Formats the website cannot parse are bridged through SRT and restored."""

import pysubs2
import pytest

from subtranslate.subtitles import (
    SubtitleError,
    prepare_subtitle,
    restore_subtitle_from_website,
    subtitle_for_website,
    verify_and_merge,
)

SSA = (
    "[Script Info]\n"
    "Title: Sample\n"
    "ScriptType: v4.00\n"
    "Collisions: Normal\n"
    "PlayResY: 480\n"
    "\n"
    "[V4 Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, "
    "BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
    "MarginV, AlphaLevel, Encoding\n"
    "Style: Default,OriginalFont,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,-1,0,1,2,2,2,"
    "10,10,10,0,0\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there\\Nfriend\n"
    "Dialogue: 0,0:00:04.50,0:00:06.25,Default,,0,0,0,,Thank you!\n"
)

VTT = (
    "WEBVTT\n"
    "Kind: captions\n"
    "\n"
    "NOTE This timing block must survive.\n"
    "\n"
    "intro\n"
    "00:00:01.000 --> 00:00:03.000 align:start position:0%\n"
    "Hello <i>world</i>\n"
    "\n"
    "00:00:04.000 --> 00:00:06.000\n"
    "Thank you!\n"
)


def test_ssa_is_translated_as_srt_and_restored_with_its_own_styles():
    source = prepare_subtitle(SSA.encode(), "movie.ssa")
    assert source.extension == ".ssa"
    assert [cue.start for cue in source.cues] == [1000, 4500]

    site_source = subtitle_for_website(source)
    assert site_source.extension == ".srt"
    assert b"Hello there\nfriend" in site_source.utf8
    assert b"00:00:01,000 --> 00:00:03,000" in site_source.utf8

    translated = site_source.utf8.replace(b"Hello there", b"Hola amigo").replace(
        b"Thank you!", b"Gracias!")
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, translated))

    text = restored.content.decode()
    assert "[V4 Styles]" in text and "OriginalFont" in text
    parsed = pysubs2.SSAFile.from_string(text, format_="ssa")
    assert [event.start for event in parsed.events] == [1000, 4500]
    assert parsed.events[0].text == "Hola amigo\\Nfriend"
    assert parsed.events[1].text == "Gracias!"
    assert restored.unchanged is False and restored.cue_count == 2


def test_utf16_sub_viewer_sub_is_still_read_as_text():
    text = ("[INFORMATION]\r\n[TITLE]Example\r\n[END INFORMATION]\r\n[SUBTITLE]\r\n"
            "00:00:01.00,00:00:03.00\r\nHello[br]world\r\n\r\n")
    source = prepare_subtitle(text.encode("utf-16"), "movie.sub")
    assert source.extension == ".sub"
    assert len(source.cues) == 1
    assert source.utf8.decode("utf-8").startswith("[INFORMATION]")
    bridged = subtitle_for_website(source)
    assert bridged.extension == ".srt" and b"Hello\nworld" in bridged.utf8


def test_binary_sub_is_explained_instead_of_silently_failing():
    with pytest.raises(SubtitleError, match="VobSub"):
        prepare_subtitle(b"\x00\x00\x01\xba\x00\x00\x01\xb9garbage", "movie.sub")


def test_vtt_is_still_uploaded_in_its_own_format():
    source = prepare_subtitle(VTT.encode(), "movie.vtt")
    assert len(source.cues) == 2
    # VTT, SBV and STL uploads keep their previous single-format path.
    assert subtitle_for_website(source) is source
    downloaded = VTT.replace("Hello <i>world</i>", "Hola mundo").encode()
    verified = verify_and_merge(source, downloaded)
    assert restore_subtitle_from_website(source, verified) is verified
    assert b"NOTE This timing block must survive." in verified.content


ASS_WITH_EMPTY = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Sign,OriginalFont,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "-1,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Sign,,0,0,0,,Hello there\n"
    "Dialogue: 0,0:00:03.50,0:00:03.50,Sign,,0,0,0,,Zero length line\n"
    "Dialogue: 0,0:00:04.00,0:00:06.00,Sign,,0,0,0,,\n"
    "Dialogue: 0,0:00:07.00,0:00:09.00,Sign,,0,0,0,,Thank you!\n"
)


def site_ass(events):
    """What the website returns: a fresh ASS with the same cues and no styles."""
    parsed = pysubs2.SSAFile()
    parsed.info["ScriptType"] = "v4.00+"
    for start, end, text in events:
        parsed.events.append(pysubs2.SSAEvent(start=start, end=end, text=text))
    return parsed.to_string("ass").encode()


def test_ass_with_empty_and_zero_length_events_is_accepted_and_restored():
    source = prepare_subtitle(ASS_WITH_EMPTY.encode(), "bcs.ass")
    assert len(source.cues) == 4          # nothing is rejected up front

    site_source = subtitle_for_website(source)
    # Only the two lines with words are uploaded.
    assert len(site_source.cues) == 2
    assert [cue.start for cue in site_source.cues] == [1000, 7000]
    assert b"Zero length line" not in site_source.utf8

    downloaded = site_ass([(1000, 3000, "Hola"), (7000, 9000, "Gracias")])
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, downloaded))

    text = restored.content.decode()
    parsed = pysubs2.SSAFile.from_string(text, format_="ass")
    assert [event.text for event in parsed.events] == [
        "Hola", "Zero length line", "", "Gracias"]
    assert [event.start for event in parsed.events] == [1000, 3500, 4000, 7000]
    assert parsed.styles["Sign"].fontname == "OriginalFont"
    assert restored.changed_cues == 2 and restored.cue_count == 4


def test_a_clean_ass_is_still_uploaded_and_merged_untouched():
    clean = ASS_WITH_EMPTY.replace("Dialogue: 0,0:00:03.50,0:00:03.50,Sign,,0,0,0,,Zero length line\n", "")
    clean = clean.replace("Dialogue: 0,0:00:04.00,0:00:06.00,Sign,,0,0,0,,\n", "")
    source = prepare_subtitle(clean.encode(), "movie.ass")
    assert subtitle_for_website(source) is source

    verified = verify_and_merge(source, site_ass([(1000, 3000, "Hola"), (7000, 9000, "Gracias")]))
    restored = restore_subtitle_from_website(source, verified)

    # The website's default style is never delivered: the original file is kept.
    text = restored.content.decode()
    assert "Style: Sign,OriginalFont" in text
    assert "Default" not in text
    parsed = pysubs2.SSAFile.from_string(text, format_="ass")
    assert [event.text for event in parsed.events] == ["Hola", "Gracias"]
    assert [event.start for event in parsed.events] == [1000, 7000]
    assert restored.changed_cues == 2


def test_ssa_with_an_empty_event_keeps_it_after_translation():
    ssa = SSA.replace(
        "Dialogue: 0,0:00:04.50,0:00:06.25,Default,,0,0,0,,Thank you!\n",
        "Dialogue: 0,0:00:04.50,0:00:06.25,Default,,0,0,0,,Thank you!\n"
        "Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,\n")
    source = prepare_subtitle(ssa.encode(), "movie.ssa")
    assert len(source.cues) == 3

    site_source = subtitle_for_website(source)
    assert site_source.extension == ".srt" and len(site_source.cues) == 2
    translated = site_source.utf8.replace(b"Hello there", b"Hola amigo").replace(
        b"Thank you!", b"Gracias!")
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, translated))

    parsed = pysubs2.SSAFile.from_string(restored.content.decode(), format_="ssa")
    assert [event.plaintext for event in parsed.events] == ["Hola amigo\nfriend", "Gracias!", ""]
    assert parsed.styles["Default"].fontname == "OriginalFont"


ASS_WITH_DRAWING = (
    "[Script Info]\nScriptType: v4.00+\n\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Sign,OriginalFont,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "-1,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Sign,,0,0,0,,Hello there\n"
    "Dialogue: 0,0:00:03.00,0:00:05.00,Sign,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}\n"
)


def test_ass_drawings_are_never_sent_for_translation():
    source = prepare_subtitle(ASS_WITH_DRAWING.encode(), "movie.ass")
    assert len(source.cues) == 2

    site_source = subtitle_for_website(source)
    assert len(site_source.cues) == 1
    assert b"m 0 0 l 100 0" not in site_source.utf8

    verified = verify_and_merge(site_source, site_ass([(1000, 3000, "Hola")]))
    restored = restore_subtitle_from_website(source, verified)
    text = restored.content.decode()
    assert "Hola" in text
    assert "m 0 0 l 100 0 100 100 0 100" in text      # the drawing is untouched
    assert text.count("Dialogue:") == 2


def test_old_ssa_marked_field_order_is_supported():
    old_style = (
        "[Script Info]\nScriptType: v4.00\n\n"
        "[V4 Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, "
        "BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, AlphaLevel, Encoding\n"
        "Style: Default,OriginalFont,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
        "-1,0,1,2,2,2,10,10,10,0,0\n\n"
        "[Events]\n"
        "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: Marked=0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there\n"
    )
    source = prepare_subtitle(old_style.encode(), "old.ssa")
    assert len(source.cues) == 1

    site_source = subtitle_for_website(source)
    assert site_source.extension == ".srt"
    translated = site_source.utf8.replace(b"Hello there", b"Hola")
    restored = restore_subtitle_from_website(source, verify_and_merge(site_source, translated))

    text = restored.content.decode()
    assert "Dialogue: Marked=0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hola" in text
    assert "[V4 Styles]" in text and "OriginalFont" in text


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_an_unreadable_ass_field_order_is_reported_not_corrupted():
    odd = (
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[Events]\n"
        "Format: Start, End, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0:00:01.00,0:00:03.00,0,Sign,,0,0,0,,Hello there\n"
    )
    with pytest.raises(SubtitleError):
        prepare_subtitle(odd.encode(), "odd.ass")


CRLF_ASS = ASS_WITH_EMPTY.replace("\n", "\r\n")


def test_crlf_ass_file_is_read_and_restored_with_its_line_endings():
    """Aegisub on Windows writes CRLF; the scanner must not depend on LF."""
    source = prepare_subtitle(CRLF_ASS.encode(), "bcs.ass")
    assert len(source.cues) == 4
    assert [cue.start for cue in source.cues] == [1000, 3500, 4000, 7000]

    site_source = subtitle_for_website(source)
    assert len(site_source.cues) == 2
    assert b"\r\n" in site_source.utf8

    verified = verify_and_merge(site_source, site_ass([(1000, 3000, "Hola"), (7000, 9000, "Gracias")]))
    restored = restore_subtitle_from_website(source, verified)

    text = restored.content.decode()
    assert "\r\n" in text and "\n\n" not in text.replace("\r\n", "")
    dialogue = [line for line in text.split("\r\n") if line.startswith("Dialogue:")]
    assert dialogue[0].endswith(",,Hola")
    assert dialogue[1].endswith(",,Zero length line")      # untouched
    assert dialogue[2].endswith(",,")                      # empty line kept
    assert dialogue[3].endswith(",,Gracias")
    assert "Style: Sign,OriginalFont" in text


def test_cr_only_ass_file_is_read():
    source = prepare_subtitle(ASS_WITH_EMPTY.replace("\n", "\r").encode(), "old.ass")
    assert len(source.cues) == 4
    assert len(subtitle_for_website(source).cues) == 2


def test_ass_text_that_mentions_the_dialogue_keyword_is_not_a_cue():
    tricky = ASS_WITH_EMPTY.replace(
        "Dialogue: 0,0:00:01.00,0:00:03.00,Sign,,0,0,0,,Hello there",
        "Dialogue: 0,0:00:01.00,0:00:03.00,Sign,,0,0,0,,Dialogue: this is spoken text")
    source = prepare_subtitle(tricky.encode(), "tricky.ass")
    assert len(source.cues) == 4
    site_source = subtitle_for_website(source)
    assert len(site_source.cues) == 2
    assert b"Dialogue: this is spoken text" in site_source.utf8
