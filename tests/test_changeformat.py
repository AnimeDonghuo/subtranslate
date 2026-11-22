"""/changeformat: the prompt, the multi-select buttons and the conversion itself."""

from types import SimpleNamespace

from support import FakeBot, FakeMessage, make_query_update, make_service, make_update

from subtranslate.convert import TARGET_CODES, FormatDraft

SRT = (b"1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n"
       b"2\n00:00:41,250 --> 00:01:05,000\nAnother line\n\n")


def document(file_name="English.srt", file_id="FILE-1", size=None):
    return SimpleNamespace(file_id=file_id, file_name=file_name,
                           file_size=len(SRT) if size is None else size)


def ready_service():
    service = make_service(owner=42)
    service.bot = FakeBot()
    service.bot.files["FILE-1"] = SRT
    return service


async def test_the_command_asks_for_a_file_first():
    service = ready_service()
    update = make_update(user_id=42, text="/changeformat")

    await service.change_format(update, None)

    assert 42 in service._format_waiting
    text = update.effective_message.reply_text.call_args.args[0]
    assert "Send the subtitle file" in text and "ASS/SSA" in text


async def test_the_next_file_shows_the_format_buttons():
    service = ready_service()
    await service.change_format(make_update(user_id=42, text="/changeformat"), None)

    update = make_update(user_id=42, document=document())
    await service.document(update, None)

    assert 42 not in service._format_waiting
    call = update.effective_message.reply_text.call_args
    assert "Select one or more formats" in call.args[0]
    buttons = [button.callback_data for row in call.kwargs["reply_markup"].inline_keyboard
               for button in row]
    assert buttons == [f"fmt:{code}" for code in TARGET_CODES] + ["fmt:done"]


async def test_replying_to_a_file_shows_the_buttons_immediately():
    service = ready_service()
    update = make_update(user_id=42, text="/changeformat")
    update.effective_message.reply_to_message = SimpleNamespace(document=document())

    await service.change_format(update, None)

    assert 42 in service._format_drafts and 42 not in service._format_waiting
    assert update.effective_message.reply_text.call_args.kwargs["reply_markup"] is not None


async def test_two_formats_are_chosen_and_both_files_are_sent_on_done():
    service = ready_service()
    service._format_drafts[42] = FormatDraft("FILE-1", "English.srt", len(SRT), 100)

    for code in ("sub", "vtt"):
        update, query = make_query_update(f"fmt:{code}")
        await service.format_callback(update, None)
        assert code in service._format_drafts[42].selected
        assert query.edits[-1]["reply_markup"] is not None

    update, query = make_query_update("fmt:done")
    await service.format_callback(update, None)

    sent = query.message.reply_document.call_args_list
    names = [call.kwargs["document"].filename for call in sent]
    assert names == ["English.sub", "English.vtt"]
    assert all("cues" in call.kwargs["caption"] for call in sent)
    assert "✅ Done" in query.message.reply_text.call_args.args[0]
    assert 42 not in service._format_drafts


async def test_tapping_the_same_format_twice_deselects_it():
    service = ready_service()
    service._format_drafts[42] = FormatDraft("FILE-1", "English.srt", len(SRT), 100)

    for _ in range(2):
        update, _query = make_query_update("fmt:sbv")
        await service.format_callback(update, None)

    assert service._format_drafts[42].selected == set()


async def test_done_with_nothing_selected_asks_for_a_choice():
    service = ready_service()
    service._format_drafts[42] = FormatDraft("FILE-1", "English.srt", len(SRT), 100)

    update, query = make_query_update("fmt:done")
    await service.format_callback(update, None)

    assert query.answers[-1][1] is True and "at least one format" in query.answers[-1][0]
    assert query.message.reply_document.await_count == 0


async def test_ass_cannot_be_chosen_from_a_crafted_callback():
    service = ready_service()
    service._format_drafts[42] = FormatDraft("FILE-1", "English.srt", len(SRT), 100)

    update, query = make_query_update("fmt:ass")
    await service.format_callback(update, None)

    assert "/ass" in query.answers[-1][0]
    assert service._format_drafts[42].selected == set()


async def test_an_expired_picker_asks_for_the_command_again():
    service = ready_service()
    update, query = make_query_update("fmt:srt")

    await service.format_callback(update, None)

    assert "/changeformat" in query.answers[-1][0]


async def test_an_ass_file_can_be_converted_to_a_clean_srt():
    service = ready_service()
    ass = (b"[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
           b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           b"Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello there\n")
    service.bot.files["ASS-1"] = ass
    message = FakeMessage(chat_id=42, message_id=100)
    await service.offer_formats(message, document("bcs.ass", "ASS-1", len(ass)))
    service._format_drafts[42].selected.add("srt")

    done, query = make_query_update("fmt:done")
    query.message = message
    await service.format_callback(done, None)

    sent = message.reply_document.call_args_list
    assert [call.kwargs["document"].filename for call in sent] == ["bcs.srt"]


async def test_a_too_large_file_is_refused_before_any_download():
    service = ready_service()
    message = FakeMessage(chat_id=42, message_id=100)

    await service.offer_formats(message, document("huge.ass", "FILE-1", 90 * 1024 * 1024))

    assert "too large" in message.reply_text.call_args.args[0]
    assert 42 not in service._format_drafts
