import pytest
import srt

from subtranslate.subtitles import (
    MAX_CUES,
    MAX_INPUT_BYTES,
    SubtitleError,
    prepare_subtitle,
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


def test_merge_preserves_source_indices_times_and_metadata():
    prepared = prepare_subtitle(SOURCE.encode())
    result = verify_and_merge(prepared, TRANSLATED.encode())
    cues = list(srt.parse(result.content.decode("utf-8")))
    assert [c.index for c in cues] == [10, 25]
    assert [(c.start, c.end) for c in cues] == [(c.start, c.end) for c in prepared.cues]
    assert cues[0].proprietary == prepared.cues[0].proprietary
    assert [c.content for c in cues] == ["Hello, world!", "Welcome!"]
    assert result.cue_count == 2
    assert not result.unchanged


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
    with pytest.raises(SubtitleError, match="large"):
        prepare_subtitle(b"x" * (MAX_INPUT_BYTES + 1))


def test_cue_limit():
    many = "".join(f"{i}\n00:00:01,000 --> 00:00:02,000\nHi\n\n" for i in range(1, MAX_CUES + 2))
    with pytest.raises(SubtitleError, match="3,000"):
        prepare_subtitle(many.encode())


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
