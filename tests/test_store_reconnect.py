"""The settings store returns to MongoDB by itself after a transient outage."""

from subtranslate.store import BanEntry, Database


class FakeMongo:
    """A MongoDB backend that can be taken down and brought back."""

    backend = "mongodb"

    def __init__(self) -> None:
        self.up = False
        self.started = False
        self.access: tuple[str | None, list[int]] = (None, [])
        self.bans: dict[int, BanEntry] = {}
        self.writes: list[tuple] = []
        self.replayed: list[tuple] = []

    async def start(self) -> None:
        if not self.up:
            raise OSError("server selection timeout")
        self.started = True

    async def ping(self) -> None:
        if not self.up:
            raise OSError("server selection timeout")

    async def close(self) -> None:
        self.started = False

    async def load_access(self):
        return self.access

    async def load_bans(self):
        return list(self.bans.values())

    async def save_access(self, mode, authorized):
        self._require_up()
        self.access = (mode, list(authorized))
        self.writes.append(("access", mode))

    async def save_ban(self, entry):
        self._require_up()
        self.bans[entry.user_id] = entry
        self.writes.append(("ban", entry.user_id))

    async def delete_ban(self, user_id):
        self._require_up()
        self.bans.pop(user_id, None)
        self.writes.append(("unban", user_id))

    async def user_info(self, user_id):
        self._require_up()
        return {}

    async def set_presets(self, user_id, codes, now):
        self._require_up()
        self.replayed.append(("set_presets", user_id, tuple(codes)))

    async def touch_user(self, user_id, name, username, now):
        self._require_up()
        self.replayed.append(("touch_user", user_id))

    async def record_translation(self, document):
        self._require_up()
        self.replayed.append(("record_translation", document.get("user_id")))

    def _require_up(self) -> None:
        if not self.up:
            raise OSError("connection reset by peer")


async def degraded_store():
    store = Database(uri="mongodb://example.invalid", default_mode="public")
    fake = FakeMongo()
    store._mongo = fake
    await store.start()
    assert store.backend == "memory" and store.degraded
    # Retries are scheduled explicitly by the tests instead of in the background.
    store._next_retry = float("inf")
    store._retry_interval = 0
    return store, fake


async def test_store_returns_to_mongodb_without_a_redeploy():
    store, fake = await degraded_store()

    fake.up = True
    await store._try_reconnect()

    assert store.backend == "mongodb"
    assert store.degraded is False
    assert fake.started is True


async def test_an_outage_that_continues_keeps_the_memory_backend():
    store, _fake = await degraded_store()

    await store._try_reconnect()          # still down

    assert store.backend == "memory" and store.degraded


async def test_mode_and_bans_changed_during_the_outage_are_pushed_back():
    store, fake = await degraded_store()

    await store.set_mode("private")
    await store.ban(7)
    assert store.is_banned(7)
    assert fake.writes == []              # nothing could reach the database

    fake.up = True
    await store._try_reconnect()

    assert store.backend == "mongodb"
    assert ("access", "private") in fake.writes
    assert 7 in fake.bans


async def test_a_ban_released_during_the_outage_is_not_resurrected():
    store, fake = await degraded_store()
    fake.bans[9] = BanEntry(user_id=9, name="Old")
    fake.up = True
    await store._try_reconnect()
    assert store.is_banned(9)

    store._degrade(OSError("connection reset by peer"))   # the outage comes back
    assert await store.release(9) is True

    fake.up = True
    await store._try_reconnect()

    assert not store.is_banned(9)
    assert 9 not in fake.bans


async def test_writes_made_during_the_outage_are_replayed():
    store, fake = await degraded_store()

    await store.set_presets(42, ["hi", "es"])
    await store.record_translation(user_id=42, name="A", username="a", language_code="hi",
                                   language_name="Hindi", cue_count=3, filename="movie.srt")

    fake.up = True
    await store._try_reconnect()

    assert ("set_presets", 42, ("hi", "es")) in fake.replayed
    assert ("record_translation", 42) in fake.replayed
    assert store._pending == []


async def test_an_operation_schedules_the_retry_in_the_background():
    store, fake = await degraded_store()
    fake.up = True
    store._next_retry = 0.0

    await store.touch_user(5, "Name", "user")

    assert store._reconnect_task is not None
    await store._reconnect_task
    assert store.backend == "mongodb"
