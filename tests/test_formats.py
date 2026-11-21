"""Formats the website cannot parse are bridged through SRT and restored."""

import pysubs2

from subtranslate.subtitles import (
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


def test_binary_sub_is_explained_instead_of_silently_failing():
    import pytest

    from subtranslate.subtitles import SubtitleError

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
