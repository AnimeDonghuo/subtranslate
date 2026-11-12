"""SRT -> ASS conversion and the /ass command (no Telegram network access)."""

from types import SimpleNamespace

from support import FakeBot, FakeMessage, context, make_service, make_update

from subtranslate import menus
from subtranslate.ass_export import ass_filename, srt_to_ass

SRT = (
    "1\n00:00:01,000 --> 00:00:03,500\n<i>Demon Dung Bombardment!</i>\n\n"
    "2\n00:01:02,240 --> 00:01:05,000\nMagic {blast}\nsecond line\n\n"
)

DOC = SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=len(SRT))


def test_convert_keeps_timings_text_and_structure():
    content, cues = srt_to_ass(SRT.encode("utf-8"))
    text = content.decode("utf-8")
    assert cues == 2
    assert text.startswith("[Script Info]")
    assert "PlayResX: 1920" in text and "PlayResY: 1080" in text
    assert "ScaledBorderAndShadow: yes" in text
    # Bold white Arial, black outline, bottom centre, raised so baked-in
    # subtitles underneath stay readable.
    assert "Style: Sigma,Arial,54,&H00FFFFFF" in text
    assert ",-1,0,0,0,100,100,0.6,0,1,3,1.5,2,80,80,120,1" in text
    assert "Dialogue: 0,0:00:01.00,0:00:03.50,Sigma,,0,0,0,,Demon Dung Bombardment!" in text
    # HTML tags are stripped, braces escaped, newlines become ASS line breaks.
    assert "<i>" not in text
    assert "Magic \\{blast\\}\\Nsecond line" in text
    assert "\r\n" in text
    # The result must be a valid ASS file for libass/ffmpeg.
    import pysubs2

    parsed = pysubs2.SSAFile.from_string(text)
    assert len(parsed.events) == 2
    assert parsed.styles["Sigma"].alignment == 2
    assert parsed.styles["Sigma"].marginv == 120
    assert parsed.events[1].start == 62_240


def test_plate_style_is_the_nearest_thing_to_a_blurred_background():
    content, _cues = srt_to_ass(SRT.encode("utf-8"), True)
    line = next(row for row in content.decode("utf-8").splitlines() if row.startswith("Style: "))
    # BorderStyle 3 = translucent box behind the text, not an outline.
    assert "&H78000000" in line
    assert ",0,3,2,0,2,80,80,120,1" in line


def test_empty_or_broken_srt_is_rejected():
    import pytest

    from subtranslate.subtitles import SubtitleError

    with pytest.raises(SubtitleError):
        srt_to_ass(b"not a subtitle")
    with pytest.raises(SubtitleError):
        srt_to_ass(b"")


def test_filenames_are_sanitised():
    assert ass_filename("movie.srt") == "movie.ass"
    assert ass_filename("C:\\videos\\my <movie>.SRT") == "my movie.ass"
    assert ass_filename("") == "subtitles.ass"
    assert len(ass_filename("x" * 300 + ".srt").encode()) <= 200


async def test_ass_prompt_then_next_srt_is_converted():
    service = make_service(owner=1)
    service.bot = FakeBot()
    service.bot.files["file1"] = SRT.encode("utf-8")
    prompt = make_update(user_id=42)
    await menus.ass_command(service, prompt, context())
    assert "SRT → ASS" in prompt.effective_message.reply_text.await_args.args[0]
    assert 42 in service._ass_waiting

    update = make_update(user_id=42, document=DOC)
    await service.document(update, None)
    sent = update.effective_message.reply_document.await_args
    assert sent.kwargs["document"].filename == "movie.ass"
    assert "2 cues" in sent.kwargs["caption"] and "ffmpeg" in sent.kwargs["caption"]
    assert 42 not in service._ass_waiting


async def test_replying_to_an_srt_with_ass_converts_immediately():
    service = make_service(owner=1)
    service.bot = FakeBot()
    service.bot.files["file1"] = SRT.encode("utf-8")
    source = FakeMessage(chat_id=42, message_id=10)
    source.document = DOC
    update = make_update(user_id=42)
    update.effective_message.reply_to_message = source

    await menus.ass_command(service, update, context("box"))
    sent = source.reply_document.await_args
    assert sent.kwargs["document"].filename == "movie.ass"
    # The plate variant is selected by the argument.
    document = sent.kwargs["document"].input_file_content.decode("utf-8")
    style = next(row for row in document.splitlines() if row.startswith("Style: "))
    assert ",0,3,2,0,2,80,80,120,1" in style and "&H78000000" in style
    assert "ffmpeg" in sent.kwargs["caption"]
    assert service._ass_waiting == set()


async def test_ass_rejects_non_srt_and_oversized_files():
    service = make_service(owner=1)
    service.bot = FakeBot()
    source = FakeMessage(chat_id=42, message_id=10)
    source.document = SimpleNamespace(file_id="x", file_name="movie.mp4", file_size=10)
    update = make_update(user_id=42)
    update.effective_message.reply_to_message = source
    await menus.ass_command(service, update, context())
    assert "other formats" in update.effective_message.reply_text.await_args.args[0]

    big = FakeMessage(chat_id=42, message_id=11)
    big.document = SimpleNamespace(file_id="y", file_name="huge.srt", file_size=26 * 1024 * 1024)
    update = make_update(user_id=42)
    update.effective_message.reply_to_message = big
    await menus.ass_command(service, update, context())
    assert "too large" in update.effective_message.reply_text.await_args.args[0]
    # Nothing was downloaded or converted for either rejected file.
    assert service.bot.files == {}
    big.reply_document.assert_not_awaited()


async def test_ass_conversion_reports_a_broken_srt():
    service = make_service(owner=1)
    service.bot = FakeBot()
    service.bot.files["file1"] = b"this is not an srt"
    update = make_update(user_id=42, document=DOC)
    service.wait_for_ass(42)
    await service.document(update, None)
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Could not convert" in reply
    update.effective_message.reply_document.assert_not_awaited()
