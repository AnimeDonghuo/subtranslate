"""Start/help text, the help topics and /ussetting preset selection."""

from types import SimpleNamespace

from support import (
    FakeMessage,
    FakeQuery,
    context,
    make_query_update,
    make_service,
    make_update,
)

from subtranslate import menus
from subtranslate.languages import language_keyboard

SITE_NAME = "translatesubtitles"


def button_labels(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def button_data(keyboard) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


async def test_start_message_has_rules_and_hides_the_service_name():
    service = make_service(owner=1)
    update = make_update(user_id=42)
    await menus.send_start(service, update, context())
    text = update.effective_message.reply_text.await_args.args[0]
    assert "Advanced Subtitle Translator" in text
    assert "permission" in text
    assert "18+" in text
    assert "banned" in text
    assert SITE_NAME not in text
    keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert "👑 Owner tools" not in button_labels(keyboard)

    owner_update = make_update(user_id=1)
    await menus.send_start(service, owner_update, context())
    owner_keyboard = owner_update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert "👑 Owner tools" in button_labels(owner_keyboard)


async def test_every_help_topic_is_reachable_and_never_names_the_service():
    for _title, body in menus.TOPICS.values():
        assert SITE_NAME not in body
    service = make_service(owner=1)
    update = make_update(user_id=1)
    await menus.send_help(service, update, context())
    text = update.effective_message.reply_text.await_args.args[0]
    assert "/ussetting" in text and "/leaderboard" in text
    keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert {"help:srt", "help:lang", "help:owner"}.issubset(set(button_data(keyboard)))


async def test_help_topic_callback_edits_the_same_message_and_back():
    service = make_service(owner=1)
    update, query = make_query_update("help:srt", user_id=1)
    await menus.help_callback(service, update, context())
    assert "SRT" in query.edits[-1]["text"]
    assert SITE_NAME not in query.edits[-1]["text"]

    update, query = make_query_update("help:menu", user_id=1)
    await menus.help_callback(service, update, context())
    assert "Help" in query.edits[-1]["text"]


async def test_owner_help_topic_is_refused_for_normal_users():
    service = make_service(owner=1)
    update, query = make_query_update("help:owner", user_id=42)
    await menus.help_callback(service, update, context())
    assert query.answers[-1][1] is True
    assert query.edits == []


async def test_ussetting_selects_multiple_languages_and_saves_them():
    service = make_service(owner=1)
    update = make_update(user_id=42)
    await menus.user_settings(service, update, context())
    text = update.effective_message.reply_text.await_args.args[0]
    assert "none selected yet" in text
    keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert "🌐 Preset languages (0)" in button_labels(keyboard)

    update, query = make_query_update("us:list:0", user_id=42)
    await menus.settings_callback(service, update, context())
    listed = button_labels(query.edits[-1]["reply_markup"])
    assert len([label for label in listed if label.startswith("▫️")]) == 6

    update, query = make_query_update("us:t:hi", user_id=42)
    await menus.settings_callback(service, update, context())
    assert "✅ Hindi" in button_labels(query.edits[-1]["reply_markup"])

    update, query = make_query_update("us:t:es", user_id=42)
    await menus.settings_callback(service, update, context())
    labels = button_labels(query.edits[-1]["reply_markup"])
    assert "✅ Spanish" in labels  # the view follows the toggled language
    assert service.preset_draft(42) == {"hi", "es"}

    update, query = make_query_update("us:save", user_id=42)
    await menus.settings_callback(service, update, context())
    assert query.answers[-1][0] == "Saved ✅"
    assert await service.store.presets(42) == ("hi", "es")
    assert "Saved" in query.edits[-1]["text"]

    # The saved presets survive a new settings screen and appear in the picker.
    update, query = make_query_update("us:open", user_id=42)
    await menus.settings_callback(service, update, context())
    assert "2 selected" in query.edits[-1]["text"]
    keyboard, _page, _pages = language_keyboard(service.catalog, presets=await service.store.presets(42))
    labels = button_labels(keyboard)
    assert "⭐ Saved languages to use (2)" in labels
    assert not any(label.startswith("⭐ Hindi") for label in labels)
    saved_button = next(button for row in keyboard.inline_keyboard for button in row
                        if button.callback_data == "preset:start")
    assert saved_button.style == "success"


async def test_clearing_presets_requires_save_to_persist():
    service = make_service(owner=1)
    await service.store.set_presets(42, ["hi"])
    update, _query = make_query_update("us:open", user_id=42)
    await menus.settings_callback(service, update, context())
    update, _query = make_query_update("us:clear", user_id=42)
    await menus.settings_callback(service, update, context())
    assert await service.store.presets(42) == ("hi",)  # not saved yet
    update, _query = make_query_update("us:save", user_id=42)
    await menus.settings_callback(service, update, context())
    assert await service.store.presets(42) == ()


async def test_preset_batch_queues_every_saved_language():
    service = make_service(owner=1)
    await service.store.set_presets(42, ["hi", "es"])
    message = FakeMessage(chat_id=42, message_id=100)
    message.reply_to_message = SimpleNamespace(
        message_id=77,
        from_user=SimpleNamespace(id=42),
        document=SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=120),
    )
    query = FakeQuery("preset:start", 42, message)
    update = SimpleNamespace(
        callback_query=query,
        effective_message=message,
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=42, full_name="Test User", username="testuser"),
    )
    await service.callback(update, None)
    job = service.queue._queue.get_nowait()
    assert job.languages == (("hi", "Hindi"), ("es", "Spanish"))
    assert (job.user_id, job.user_name, job.username) == (42, "Test User", "testuser")
    assert query.answers[-1][0].startswith("Queued")
    service.queue._queue.task_done()


async def test_preset_batch_without_saved_languages_is_refused():
    service = make_service(owner=1)
    message = FakeMessage(chat_id=42, message_id=100)
    message.reply_to_message = SimpleNamespace(
        message_id=77, from_user=SimpleNamespace(id=42),
        document=SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=120),
    )
    query = FakeQuery("preset:start", 42, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=SimpleNamespace(id=42, full_name="U", username=None))
    await service.callback(update, None)
    assert query.answers[-1][1] is True
    assert "no saved preset" in query.answers[-1][0]
    assert service.queue._queue.empty()


async def test_language_picker_page_edits_keep_saved_presets_visible():
    service = make_service(owner=1)
    await service.store.set_presets(42, ["hi"])
    message = FakeMessage(chat_id=42, message_id=100)
    query = FakeQuery("page:1", 42, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=42, type="private"),
                             effective_user=SimpleNamespace(id=42, full_name="U", username=None))
    await service.callback(update, None)
    keyboard = query.edits[-1]["reply_markup"]
    assert "⭐ Saved languages to use (1)" in button_labels(keyboard)
    # Free users see the exclusive select-all button locked.
    assert any(button.callback_data == "all:locked" for row in keyboard.inline_keyboard for button in row)
    assert not any(button.callback_data == "all:confirm:1" for row in keyboard.inline_keyboard for button in row)


def styles(keyboard) -> dict[str, str]:
    return {button.text: button.style for row in keyboard.inline_keyboard for button in row}


async def test_buttons_use_matching_colours():
    service = make_service(owner=1)
    update = make_update(user_id=1)
    await menus.send_start(service, update, context())
    start_styles = styles(update.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert start_styles["📄 How to translate"] == "success"
    assert start_styles["🌐 Languages & presets"] == "primary"
    assert start_styles["👑 Owner tools"] == "danger"

    update = make_update(user_id=1)
    await menus.user_settings(service, update, context())
    settings_styles = styles(update.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert settings_styles["💾 Save"] == "success"
    assert settings_styles["🌐 Preset languages (0)"] == "primary"

    update, query = make_query_update("us:list:0", user_id=1)
    await menus.settings_callback(service, update, context())
    list_styles = styles(query.edits[-1]["reply_markup"])
    assert "✅" not in "".join(text for text in list_styles)  # nothing selected yet
    assert list_styles["💾 Save"] == "success" and list_styles["🧹 Clear all"] == "danger"
    assert list_styles["▫️ Afrikaans"] == "primary"

    update, query = make_query_update("us:t:af", user_id=1)
    await menus.settings_callback(service, update, context())
    list_styles = styles(query.edits[-1]["reply_markup"])
    assert list_styles["✅ Afrikaans"] == "success"

    update, query = make_query_update("help:srt", user_id=1)
    await menus.help_callback(service, update, context())
    assert styles(query.edits[-1]["reply_markup"])["🔙 Back to help"] == "primary"


async def test_each_user_sees_and_runs_only_their_own_saved_languages():
    from types import SimpleNamespace

    service = make_service(owner=1)
    await service.store.set_presets(42, ["hi"])
    await service.store.set_presets(43, ["es", "ja"])
    document = SimpleNamespace(file_id="file1", file_name="movie.srt", file_size=120)

    first = make_update(user_id=42, document=document)
    await service.document(first, None)
    first_labels = button_labels(first.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert "⭐ Saved languages to use (1)" in first_labels

    second = make_update(user_id=43, document=document)
    await service.document(second, None)
    second_labels = button_labels(second.effective_message.reply_text.await_args.kwargs["reply_markup"])
    assert "⭐ Saved languages to use (2)" in second_labels

    # The second user's button runs only that user's saved set.
    message = FakeMessage(chat_id=43, message_id=101)
    message.reply_to_message = SimpleNamespace(
        message_id=78, from_user=SimpleNamespace(id=43), document=document,
    )
    query = FakeQuery("preset:start", 43, message)
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=43, type="private"),
                             effective_user=SimpleNamespace(id=43, full_name="Other", username=None))
    await service.callback(update, None)
    job = service.queue._queue.get_nowait()
    assert job.languages == (("es", "Spanish"), ("ja", "Japanese"))
    service.queue._queue.task_done()

    # A user without presets never sees the button.
    third = make_update(user_id=44, document=document)
    await service.document(third, None)
    assert not any(label.startswith("⭐") for label in
                   button_labels(third.effective_message.reply_text.await_args.kwargs["reply_markup"]))
