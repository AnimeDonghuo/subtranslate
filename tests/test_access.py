"""Public/private mode, authorization and ban enforcement."""

import asyncio

from support import context, make_query_update, make_service, make_update

from subtranslate import admin

OWNER = 1
STRANGER = 42


def reply(update) -> str:
    return update.effective_message.reply_text.await_args.args[0]


async def test_default_mode_follows_allowed_user_ids():
    open_service = make_service(owner=OWNER)
    assert open_service.store.mode == "public"
    assert await open_service.ensure_allowed(make_update(user_id=STRANGER)) is True

    restricted = make_service(owner=OWNER, allowed={STRANGER})
    assert restricted.store.mode == "private"
    assert await restricted.ensure_allowed(make_update(user_id=STRANGER)) is True
    other = make_update(user_id=777)
    assert await restricted.ensure_allowed(other) is False
    assert "private" in reply(other)


async def test_private_mode_blocks_until_owner_authorizes():
    service = make_service(owner=OWNER)
    owner_update = make_update(user_id=OWNER)
    await admin.private_command(service, owner_update, context())
    assert service.store.mode == "private"
    assert "Private mode" in reply(owner_update)

    blocked = make_update(user_id=STRANGER)
    assert await service.ensure_allowed(blocked) is False
    assert "🔒" in reply(blocked)

    await admin.auth_command(service, make_update(user_id=OWNER), context(str(STRANGER)))
    assert service.store.is_authorized(STRANGER)
    assert await service.ensure_allowed(make_update(user_id=STRANGER)) is True

    await admin.unauth_command(service, make_update(user_id=OWNER), context(str(STRANGER)))
    assert not service.store.is_authorized(STRANGER)
    assert await service.ensure_allowed(make_update(user_id=STRANGER)) is False

    await admin.public_command(service, make_update(user_id=OWNER), context())
    assert service.store.mode == "public"
    assert await service.ensure_allowed(make_update(user_id=STRANGER)) is True


async def test_ban_applies_in_public_mode_and_owner_cannot_be_banned():
    service = make_service(owner=OWNER)
    await service.store.touch_user(STRANGER, "Annoying User", "annoying")

    await admin.ban_command(service, make_update(user_id=OWNER), context(str(STRANGER)))
    assert service.store.is_banned(STRANGER)

    banned = make_update(user_id=STRANGER)
    assert await service.ensure_allowed(banned) is False
    assert "banned" in reply(banned)

    await admin.ban_command(service, make_update(user_id=OWNER), context(str(OWNER)))
    assert not service.store.is_banned(OWNER)
    assert await service.ensure_allowed(make_update(user_id=OWNER)) is True

    await admin.release_command(service, make_update(user_id=OWNER), context(str(STRANGER)))
    assert not service.store.is_banned(STRANGER)
    assert await service.ensure_allowed(make_update(user_id=STRANGER)) is True


async def test_banned_user_is_refused_on_buttons_too():
    service = make_service(owner=OWNER)
    await service.store.ban(STRANGER, OWNER)
    _update, query = make_query_update("help:srt", user_id=STRANGER)
    assert await service.ensure_callback_allowed(query) is False
    assert query.answers and "banned" in query.answers[0][0]
    assert query.answers[0][1] is True  # shown as an alert


async def test_owner_only_commands_refuse_other_users():
    service = make_service(owner=OWNER)
    stranger = make_update(user_id=STRANGER)
    await admin.ban_command(service, stranger, context("5"))
    assert "only for the bot owner" in reply(stranger)
    assert not service.store.is_banned(5)

    await admin.private_command(service, make_update(user_id=STRANGER), context())
    assert service.store.mode == "public"

    # With no OWNER_ID configured the command explains how to fix it.
    unowned = make_service(owner=None)
    update = make_update(user_id=STRANGER)
    await admin.ban_command(unowned, update, context("5"))
    assert "OWNER_ID" in reply(update)


async def test_ban_stops_queued_and_active_work_immediately():
    from subtranslate.jobs import TranslationJob

    service = make_service(owner=OWNER)
    active = asyncio.Event()
    service._single_cancellations[STRANGER] = active
    service.queue.submit(TranslationJob(
        chat_id=STRANGER, original_message_id=1, file_id="f", file_name="a.srt",
        file_size=10, language_code="hi", language_name="Hindi",
    ))
    update = make_update(user_id=OWNER)
    await admin.ban_command(service, update, context(str(STRANGER)))
    assert active.is_set()
    assert service.queue._queue.empty()
    assert "Stopped 1 queued job(s)" in reply(update)


async def test_ban_requires_a_numeric_id_and_never_echoes_garbage():
    service = make_service(owner=OWNER)
    update = make_update(user_id=OWNER)
    await admin.ban_command(service, update, context("../../etc/passwd"))
    assert "Usage: /ban" in reply(update)
    assert "/etc/passwd" not in reply(update)
    await admin.ban_command(service, update, context())
    assert "Usage: /ban" in reply(update)
