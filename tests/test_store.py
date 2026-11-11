"""Persistence, fallback and aggregation behaviour of the settings store."""

from datetime import UTC, datetime, timedelta

import pytest

from subtranslate import store as store_module
from subtranslate.store import Database, period_start


def test_period_boundaries_are_utc_calendar_periods():
    moment = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)  # Wednesday
    assert period_start("day", moment) == datetime(2026, 11, 11, tzinfo=UTC)
    assert period_start("week", moment) == datetime(2026, 11, 9, tzinfo=UTC)
    assert period_start("month", moment) == datetime(2026, 11, 1, tzinfo=UTC)
    assert period_start("year", moment) == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        period_start("decade", moment)


async def test_access_mode_authorization_and_bans_are_cached():
    database = Database(default_mode="private")
    await database.start()
    assert database.mode == "private" and database.backend == "memory"

    await database.set_mode("public")
    assert database.mode == "public"

    assert not database.is_authorized(42)
    await database.authorize(42)
    assert database.is_authorized(42) and database.authorized == frozenset({42})
    await database.unauthorize(42)
    assert not database.is_authorized(42)

    await database.touch_user(555, "Spammer", "spam_account")
    entry = await database.ban(555, 1)
    assert entry.name == "Spammer" and entry.username == "spam_account"
    assert database.is_banned(555)
    assert [item.user_id for item in database.banned] == [555]
    assert await database.release(555) is True
    assert not database.is_banned(555)
    await database.close()


async def test_presets_and_user_profile_round_trip():
    database = Database(default_mode="public")
    await database.start()
    await database.touch_user(42, "Asha", "asha")
    assert await database.presets(42) == ()
    await database.set_presets(42, ["hi", "es", "zh-CN"])
    assert await database.presets(42) == ("hi", "es", "zh-CN")
    info = await database.user_info(42)
    assert info["name"] == "Asha" and info["username"] == "asha"
    assert await database.presets(None) == ()
    await database.close()


async def test_translation_counts_leaderboard_and_rank():
    database = Database(default_mode="public")
    await database.start()
    for _ in range(3):
        await database.record_translation(
            user_id=1, name="Owner", username="owner", language_code="hi",
            language_name="Hindi", cue_count=10, filename="a.Hindi.srt",
        )
    for _ in range(5):
        await database.record_translation(
            user_id=7, name="Power User", username="power", language_code="es",
            language_name="Spanish", cue_count=10, filename="b.Spanish.srt",
        )
    await database.record_translation(
        user_id=8, name="New User", username="", language_code="fr",
        language_name="French", cue_count=1, filename="c.French.srt",
    )

    await database.touch_user(1, "Owner", "owner")
    await database.touch_user(7, "Power User", "power")
    stats = await database.stats()
    assert stats.translations_total == 9
    assert stats.translations_day == 9 and stats.translations_month == 9
    assert stats.users_total == 2 and stats.users_active_7d == 2

    board = await database.leaderboard("month", limit=10)
    assert [(row.user_id, row.count) for row in board] == [(7, 5), (1, 3), (8, 1)]
    assert board[0].name == "Power User" and board[0].username == "power"

    assert await database.rank("month", 7) == (1, 5)
    assert await database.rank("month", 8) == (3, 1)
    assert await database.rank("month", 99) == (None, 0)
    assert await database.user_counts(7) == {"total": 5, "day": 5, "week": 5, "month": 5, "year": 5}
    await database.close()


async def test_leaderboard_only_counts_the_requested_period():
    database = Database(default_mode="public")
    await database.start()
    old = datetime.now(UTC) - timedelta(days=40)
    await database.record_translation(
        user_id=9, name="Old", username="", language_code="de",
        language_name="German", cue_count=1, filename="old.srt", when=old,
    )
    await database.record_translation(
        user_id=1, name="Fresh", username="", language_code="hi",
        language_name="Hindi", cue_count=1, filename="x.srt",
    )
    assert [row.user_id for row in await database.leaderboard("month")] == [1]
    assert [row.user_id for row in await database.leaderboard("year")] == [1, 9]
    await database.close()


async def test_unreachable_mongodb_degrades_to_memory(monkeypatch):
    async def fail(self):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(store_module._MongoBackend, "start", fail)
    database = Database(default_mode="private", uri="mongodb://user:secret@example.invalid:27017")
    await database.start()
    assert database.degraded is True
    assert database.backend == "memory"
    # The bot must still honour bans, presets and the default mode.
    await database.set_mode("public")
    await database.set_presets(5, ["hi"])
    assert await database.presets(5) == ("hi",)
    await database.ban(6, 1)
    assert database.is_banned(6)
    await database.close()


class _BrokenBackend:
    async def close(self):
        return None

    async def record_translation(self, document):
        raise ConnectionError("mongo down")


async def test_a_failing_operation_switches_to_memory_without_raising(caplog):
    database = Database(default_mode="private")
    await database.start()
    database._active = _BrokenBackend()
    await database.record_translation(
        user_id=1, name="A", username="", language_code="hi",
        language_name="Hindi", cue_count=1, filename="a.srt",
    )
    assert database.degraded is True and database.backend == "memory"
    assert "MongoDB operation failed" in caplog.text
    await database.close()
