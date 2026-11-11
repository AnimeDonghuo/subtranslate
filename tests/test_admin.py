"""Owner commands: ban list, statistics and the leaderboard."""

from support import context, make_query_update, make_service, make_update

from subtranslate import admin

OWNER = 1


def message_text(update) -> str:
    return update.effective_message.reply_text.await_args.args[0]


def labels(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def callbacks(keyboard) -> set[str]:
    return {button.callback_data for row in keyboard.inline_keyboard for button in row}


async def test_banlist_shows_names_usernames_and_ids_newest_first():
    service = make_service(owner=OWNER)
    await service.store.touch_user(555, "Spam King", "spamking")
    await service.store.touch_user(777, "Silent User", "")
    await service.store.ban(555, OWNER)
    await service.store.ban(777, OWNER)

    update = make_update(user_id=OWNER)
    await admin.banlist_command(service, update, context())
    text = message_text(update)
    assert "Banned users (2)" in text
    # Newest ban first.
    assert "1. Silent User · <code>777</code>" in text
    assert "2. Spam King (@spamking) · <code>555</code>" in text
    assert "since" in text
    assert "/release" in text

    empty = make_service(owner=OWNER)
    update = make_update(user_id=OWNER)
    await admin.banlist_command(empty, update, context())
    assert "ban list is empty" in message_text(update).lower()


async def test_ban_and_release_report_the_target_user():
    service = make_service(owner=OWNER)
    await service.store.touch_user(555, "Spam King", "spamking")
    ban = make_update(user_id=OWNER)
    await admin.ban_command(service, ban, context("555"))
    assert "Spam King (@spamking)" in message_text(ban)
    assert "/release 555" in message_text(ban)

    again = make_update(user_id=OWNER)
    await admin.release_command(service, again, context("555"))
    assert "Released" in message_text(again)
    missing = make_update(user_id=OWNER)
    await admin.release_command(service, missing, context("999"))
    assert "not banned" in message_text(missing)


async def test_authorization_messages_and_env_protection():
    service = make_service(owner=OWNER, allowed={123})
    first = make_update(user_id=OWNER)
    await admin.auth_command(service, first, context("123"))
    assert "Authorized" in message_text(first)

    env_user = make_update(user_id=OWNER)
    await admin.unauth_command(service, env_user, context("123"))
    assert "ALLOWED_USER_IDS" in message_text(env_user)

    added = make_update(user_id=OWNER)
    await admin.auth_command(service, added, context("456"))
    assert "Authorized" in message_text(added)
    assert service.store.is_authorized(456)

    duplicate = make_update(user_id=OWNER)
    await admin.auth_command(service, duplicate, context("456"))
    assert "already authorized" in message_text(duplicate)

    removed = make_update(user_id=OWNER)
    await admin.unauth_command(service, removed, context("456"))
    assert "Removed authorization" in message_text(removed)

    missing = make_update(user_id=OWNER)
    await admin.unauth_command(service, missing, context("321"))
    assert "not authorized" in message_text(missing)


async def test_owner_stats_include_access_bans_and_mongo_space():
    service = make_service(owner=OWNER)
    await service.store.touch_user(42, "Asha", "asha")
    await service.store.record_translation(
        user_id=42, name="Asha", username="asha", language_code="hi",
        language_name="Hindi", cue_count=3, filename="a.Hindi.srt",
    )
    await service.store.ban(555, OWNER)

    update = make_update(user_id=OWNER)
    await admin.stats_command(service, update, context())
    text = message_text(update)
    assert "Bot statistics" in text
    assert "Users: <b>2</b>" in text  # Asha and the owner who ran /stats
    assert "Banned: <b>1</b>" in text
    assert "Subtitles translated: <b>1</b>" in text
    assert "MongoDB storage" in text
    assert "Mode:" in text
    assert "in-memory" in text  # no MONGODB_URI in this test


async def test_user_stats_are_personal_and_hide_owner_details():
    service = make_service(owner=OWNER)
    for _ in range(2):
        await service.store.record_translation(
            user_id=42, name="Asha", username="asha", language_code="hi",
            language_name="Hindi", cue_count=1, filename="a.srt",
        )
    update = make_update(user_id=42)
    await admin.stats_command(service, update, context())
    text = message_text(update)
    assert "Your statistics" in text
    assert "Subtitles translated: <b>2</b>" in text
    assert "MongoDB" not in text
    assert "Banned" not in text


async def test_leaderboard_shows_top_ten_with_ids_names_and_period_buttons():
    service = make_service(owner=OWNER)
    for user_id in range(100, 112):  # 12 users, more than one page worth
        for _ in range(user_id - 99):
            await service.store.record_translation(
                user_id=user_id, name=f"User {user_id}", username=f"user{user_id}",
                language_code="hi", language_name="Hindi", cue_count=1,
                filename=f"{user_id}.srt",
            )
    await service.store.record_translation(
        user_id=42, name="Me", username="me", language_code="hi",
        language_name="Hindi", cue_count=1, filename="me.srt",
    )

    update = make_update(user_id=42)
    await admin.leaderboard_command(service, update, context())
    text = message_text(update)
    assert "Leaderboard — This month" in text
    assert "🥇" in text and "🥈" in text and "🥉" in text
    assert "@user111" in text and "<code>111</code>" in text
    assert text.count(" srt") >= 10
    assert "Your rank" in text
    keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert callbacks(keyboard) == {"lb:day", "lb:week", "lb:month", "lb:year"}
    assert any("✅" in label and "Month" in label for label in labels(keyboard))


async def test_leaderboard_period_button_switches_the_view():
    service = make_service(owner=OWNER)
    await service.store.record_translation(
        user_id=42, name="Me", username="me", language_code="hi",
        language_name="Hindi", cue_count=1, filename="me.srt",
    )
    update, query = make_query_update("lb:day", user_id=42)
    await admin.leaderboard_callback(service, update, context())
    edit = query.edits[-1]
    assert "Leaderboard — Today" in edit["text"]
    assert any("✅" in label and "Today" in label for label in labels(edit["reply_markup"]))

    update, query = make_query_update("lb:year", user_id=42)
    await admin.leaderboard_callback(service, update, context())
    assert "Leaderboard — This year" in query.edits[-1]["text"]


async def test_empty_leaderboard_is_explained():
    service = make_service(owner=OWNER)
    update = make_update(user_id=42)
    await admin.leaderboard_command(service, update, context())
    text = message_text(update)
    assert "No subtitles have been translated" in text
    assert "not translated anything" in text


async def test_public_and_private_commands_persist_the_mode():
    service = make_service(owner=OWNER)
    await admin.public_command(service, make_update(user_id=OWNER), context())
    assert service.store.mode == "public"
    await admin.private_command(service, make_update(user_id=OWNER), context())
    assert service.store.mode == "private"
