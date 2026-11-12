"""Persistent multi-user settings, access control and usage counters.

MongoDB is optional. When ``MONGODB_URI`` is missing or the database is briefly
unreachable, the bot keeps working with an in-memory backend, so a translation
never fails because of bookkeeping.

Access rules, the ban list and the user's language presets are cached here, so
routine permission checks are synchronous and never wait for the network.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover - the deployment image always installs it
    MongoClient = None

logger = logging.getLogger(__name__)
MODES = frozenset({"public", "private"})
PERIODS = ("day", "week", "month", "year")
# A deployment without MongoDB keeps recent usage in memory only.
_MEMORY_TRANSLATIONS = 50_000


def utcnow() -> datetime:
    return datetime.now(UTC)


def period_start(period: str, now: datetime | None = None) -> datetime:
    """Start of the current UTC day/week/month/year."""
    moment = (now or utcnow()).astimezone(UTC)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "year":
        return day.replace(month=1, day=1)
    raise ValueError("Unknown period")


@dataclass(frozen=True)
class BanEntry:
    user_id: int
    name: str = ""
    username: str = ""
    banned_at: datetime | None = None
    banned_by: int | None = None


@dataclass(frozen=True)
class LeaderEntry:
    user_id: int
    name: str
    username: str
    count: int


@dataclass(frozen=True)
class Stats:
    users_total: int = 0
    users_active_7d: int = 0
    banned: int = 0
    authorized: int = 0
    mode: str = "private"
    translations_total: int = 0
    translations_day: int = 0
    translations_week: int = 0
    translations_month: int = 0
    translations_year: int = 0
    storage_used: int | None = None
    storage_total: int | None = None
    backend: str = "memory"


class _MemoryBackend:
    """Small, dependency-free storage used without MongoDB and as a fallback."""

    backend = "memory"

    def __init__(self) -> None:
        self._access: tuple[str, list[int]] | None = None
        self._bans: dict[int, BanEntry] = {}
        self._users: dict[int, dict] = {}
        self._presets: dict[int, list[str]] = {}
        self._translations: list[dict] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def load_access(self) -> tuple[str | None, list[int]]:
        mode, authorized = self._access or (None, [])
        return mode, list(authorized)

    async def save_access(self, mode: str, authorized: list[int]) -> None:
        self._access = (mode, list(authorized))

    async def load_bans(self) -> list[BanEntry]:
        return list(self._bans.values())

    async def save_ban(self, entry: BanEntry) -> None:
        self._bans[entry.user_id] = entry

    async def delete_ban(self, user_id: int) -> None:
        self._bans.pop(user_id, None)

    async def user_info(self, user_id: int) -> dict | None:
        return dict(self._users[user_id]) if user_id in self._users else None

    async def users_info(self, user_ids: list[int]) -> dict[int, dict]:
        return {user_id: dict(self._users[user_id]) for user_id in user_ids if user_id in self._users}

    async def user_ids(self) -> tuple[int, ...]:
        return tuple(self._users)

    async def premium_until(self, user_id: int) -> datetime | None:
        value = self._users.get(user_id, {}).get("premium_until")
        return value if isinstance(value, datetime) else None

    async def set_premium(self, user_id: int, until: datetime, now: datetime) -> None:
        self._users.setdefault(user_id, {"first_seen": now}).update(premium_until=until, last_seen=now)

    async def clear_premium(self, user_id: int, now: datetime) -> bool:
        user = self._users.get(user_id)
        if not user or "premium_until" not in user:
            return False
        user.pop("premium_until", None)
        user["last_seen"] = now
        return True

    async def touch_user(self, user_id: int, name: str, username: str, now: datetime) -> None:
        user = self._users.setdefault(user_id, {"first_seen": now})
        user.update(name=name, username=username, last_seen=now)

    async def get_presets(self, user_id: int) -> list[str]:
        return list(self._presets.get(user_id, ()))

    async def set_presets(self, user_id: int, codes: list[str], now: datetime) -> None:
        self._presets[user_id] = list(codes)
        self._users.setdefault(user_id, {"first_seen": now}).update(last_seen=now)

    async def record_translation(self, document: dict) -> None:
        self._translations.append(document)
        if len(self._translations) > _MEMORY_TRANSLATIONS:
            del self._translations[: len(self._translations) - _MEMORY_TRANSLATIONS]

    async def count_translations(self, start: datetime | None, end: datetime | None,
                                 user_id: int | None) -> int:
        return sum(
            1 for item in self._translations
            if (start is None or item["created_at"] >= start)
            and (end is None or item["created_at"] < end)
            and (user_id is None or item["user_id"] == user_id)
        )

    async def group_counts(self, start: datetime) -> list[dict]:
        grouped: dict[int, dict] = {}
        for item in self._translations:
            if item["created_at"] < start:
                continue
            row = grouped.setdefault(item["user_id"], {"user_id": item["user_id"], "count": 0,
                                                       "name": "", "username": ""})
            row["count"] += 1
            row["name"] = item.get("name") or row["name"]
            row["username"] = item.get("username") or row["username"]
        return list(grouped.values())

    async def total_users(self) -> int:
        return len(self._users)

    async def active_users(self, since: datetime) -> int:
        return sum(1 for user in self._users.values() if user.get("last_seen", since) >= since)

    async def storage(self) -> tuple[int | None, int | None]:
        return None, None


class _MongoBackend:
    """MongoDB storage; every driver call runs in a worker thread."""

    backend = "mongodb"

    def __init__(self, uri: str, database: str, storage_limit_mb: int) -> None:
        self._uri = uri
        self._database = database
        self._storage_limit_mb = storage_limit_mb
        self._client = None
        self._db = None

    @property
    def users(self):
        return self._db.users

    @property
    def bans(self):
        return self._db.bans

    @property
    def access(self):
        return self._db.access

    @property
    def translations(self):
        return self._db.translations

    async def start(self) -> None:
        if MongoClient is None:
            raise RuntimeError("pymongo is not installed")
        client = MongoClient(
            self._uri,
            maxPoolSize=5,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=8_000,
            appname="subtranslate",
        )
        try:
            await asyncio.to_thread(client.admin.command, "ping")
            db = client[self._database]
            await asyncio.to_thread(db.users.create_index, "user_id", unique=True)
            await asyncio.to_thread(db.users.create_index, "last_seen")
            await asyncio.to_thread(db.bans.create_index, "user_id", unique=True)
            await asyncio.to_thread(db.translations.create_index, [("user_id", 1), ("created_at", -1)])
            await asyncio.to_thread(db.translations.create_index, [("created_at", -1)])
            self._client, self._db = client, db
        except BaseException:
            await asyncio.to_thread(client.close)
            raise

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = self._db = None

    async def load_access(self) -> tuple[str | None, list[int]]:
        document = await asyncio.to_thread(self.access.find_one, {"_id": "access"})
        if not document:
            return None, []
        return document.get("mode"), [int(value) for value in document.get("authorized") or []]

    async def save_access(self, mode: str, authorized: list[int]) -> None:
        await asyncio.to_thread(
            self.access.update_one,
            {"_id": "access"},
            {"$set": {"mode": mode, "authorized": list(authorized)}},
            upsert=True,
        )

    async def load_bans(self) -> list[BanEntry]:
        documents = await asyncio.to_thread(lambda: list(self.bans.find({})))
        return [
            BanEntry(
                user_id=int(item["user_id"]),
                name=item.get("name") or "",
                username=item.get("username") or "",
                banned_at=item.get("banned_at"),
                banned_by=item.get("banned_by"),
            )
            for item in documents
        ]

    async def save_ban(self, entry: BanEntry) -> None:
        await asyncio.to_thread(
            self.bans.update_one,
            {"user_id": entry.user_id},
            {"$set": {"name": entry.name, "username": entry.username,
                      "banned_at": entry.banned_at, "banned_by": entry.banned_by}},
            upsert=True,
        )

    async def delete_ban(self, user_id: int) -> None:
        await asyncio.to_thread(self.bans.delete_one, {"user_id": user_id})

    async def user_info(self, user_id: int) -> dict | None:
        return await asyncio.to_thread(self.users.find_one, {"user_id": user_id}, {"_id": 0})

    async def users_info(self, user_ids: list[int]) -> dict[int, dict]:
        documents = await asyncio.to_thread(
            lambda: list(self.users.find({"user_id": {"$in": list(user_ids)}}, {"_id": 0}))
        )
        return {int(item["user_id"]): item for item in documents}

    async def touch_user(self, user_id: int, name: str, username: str, now: datetime) -> None:
        await asyncio.to_thread(
            self.users.update_one,
            {"user_id": user_id},
            {"$set": {"name": name, "username": username, "last_seen": now},
             "$setOnInsert": {"first_seen": now, "presets": []}},
            upsert=True,
        )

    async def get_presets(self, user_id: int) -> list[str]:
        document = await asyncio.to_thread(self.users.find_one, {"user_id": user_id}, {"presets": 1})
        return list((document or {}).get("presets") or [])

    async def set_presets(self, user_id: int, codes: list[str], now: datetime) -> None:
        await asyncio.to_thread(
            self.users.update_one,
            {"user_id": user_id},
            {"$set": {"presets": list(codes), "last_seen": now},
             "$setOnInsert": {"first_seen": now, "name": "", "username": ""}},
            upsert=True,
        )

    async def record_translation(self, document: dict) -> None:
        await asyncio.to_thread(self.translations.insert_one, dict(document))

    async def count_translations(self, start: datetime | None, end: datetime | None,
                                 user_id: int | None) -> int:
        query: dict = {}
        if start is not None or end is not None:
            window = {}
            if start is not None:
                window["$gte"] = start
            if end is not None:
                window["$lt"] = end
            query["created_at"] = window
        if user_id is not None:
            query["user_id"] = user_id
        return int(await asyncio.to_thread(self.translations.count_documents, query))

    async def group_counts(self, start: datetime) -> list[dict]:
        pipeline = [
            {"$match": {"created_at": {"$gte": start}}},
            {"$group": {"_id": "$user_id", "count": {"$sum": 1},
                        "name": {"$last": "$name"}, "username": {"$last": "$username"}}},
        ]
        documents = await asyncio.to_thread(lambda: list(self.translations.aggregate(pipeline)))
        return [
            {"user_id": int(item["_id"]), "count": int(item["count"]),
             "name": item.get("name") or "", "username": item.get("username") or ""}
            for item in documents
        ]

    async def total_users(self) -> int:
        return int(await asyncio.to_thread(self.users.count_documents, {}))

    async def active_users(self, since: datetime) -> int:
        return int(await asyncio.to_thread(self.users.count_documents, {"last_seen": {"$gte": since}}))

    async def storage(self) -> tuple[int | None, int | None]:
        stats = await asyncio.to_thread(self._db.command, "dbStats")
        used = int(stats.get("storageSize", 0)) + int(stats.get("indexSize", 0))
        if self._storage_limit_mb > 0:
            # Atlas/Koyeb free tiers cap storage, while the shared volume size
            # is not the plan's quota. Report the configured plan limit.
            return used, self._storage_limit_mb * 1024 * 1024
        total = int(stats.get("fsTotalSize") or 0)
        if total > 0:
            return int(stats.get("fsUsedSize") or 0), total
        return used, None


class Database:
    """Facade over MongoDB with a transparent in-memory fallback."""

    def __init__(self, *, default_mode: str = "private", uri: str = "",
                 database: str = "subtranslate", storage_limit_mb: int = 512) -> None:
        self._default_mode = default_mode if default_mode in MODES else "private"
        self._mode = self._default_mode
        self._authorized: set[int] = set()
        self._bans: dict[int, BanEntry] = {}
        self._memory = _MemoryBackend()
        self._mongo = (_MongoBackend(uri, database, storage_limit_mb)
                       if uri and MongoClient is not None else None)
        self._active: _MemoryBackend | _MongoBackend = self._memory
        self.db_configured = bool(uri)
        self.degraded = False

    # -- connection -----------------------------------------------------
    async def start(self) -> None:
        if self._mongo is None:
            if self.db_configured:
                logger.warning("MongoDB support unavailable (pymongo missing); using in-memory settings")
            return
        try:
            await self._mongo.start()
            mode, authorized = await self._mongo.load_access()
            bans = await self._mongo.load_bans()
        except Exception as exc:  # noqa: BLE001 - credentials may appear in driver errors
            logger.warning("MongoDB unavailable (%s); using in-memory settings", type(exc).__name__)
            self.degraded = True
            try:
                await self._mongo.close()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.info("MongoDB cleanup after a failed start failed (%s)", type(exc).__name__)
            return
        self._active = self._mongo
        if mode in MODES:
            self._mode = mode
        if authorized:
            self._authorized = {value for value in authorized if isinstance(value, int) and value > 0}
        self._bans = {entry.user_id: entry for entry in bans}
        if mode not in MODES:
            await self._persist_access()
        logger.info("MongoDB ready: mode=%s, authorized=%d, banned=%d",
                    self._mode, len(self._authorized), len(self._bans))

    async def close(self) -> None:
        for backend in (self._active, self._memory):
            try:
                await backend.close()
            except Exception as exc:  # noqa: BLE001 - never fail shutdown on cleanup
                logger.warning("Settings store cleanup failed (%s)", type(exc).__name__)

    def _degrade(self, exc: Exception) -> None:
        if self._active is not self._memory:
            logger.warning("MongoDB operation failed (%s); using in-memory settings until restart",
                           type(exc).__name__)
            self._active = self._memory
            self.degraded = True

    async def _run(self, method: str, *args, default=None):
        try:
            return await getattr(self._active, method)(*args)
        except Exception as exc:  # noqa: BLE001 - the bot must survive database errors
            if self._active is self._memory:
                logger.warning("Settings store operation failed (%s)", type(exc).__name__)
                return default
            self._degrade(exc)
            try:
                return await getattr(self._memory, method)(*args)
            except Exception as fallback:  # noqa: BLE001 - bookkeeping is never fatal
                logger.warning("Settings store fallback failed (%s)", type(fallback).__name__)
                return default

    async def _persist_access(self) -> None:
        try:
            await self._active.save_access(self._mode, sorted(self._authorized))
        except Exception as exc:  # noqa: BLE001 - cached access still applies
            self._degrade(exc)

    # -- access ---------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def authorized(self) -> frozenset[int]:
        return frozenset(self._authorized)

    @property
    def banned(self) -> tuple[BanEntry, ...]:
        # Newest ban first; equal timestamps fall back to the user ID so the
        # list stays stable for /banlist.
        return tuple(sorted(
            self._bans.values(),
            key=lambda entry: (entry.banned_at or datetime.min.replace(tzinfo=UTC), -entry.user_id),
            reverse=True,
        ))

    @property
    def backend(self) -> str:
        return "mongodb" if self._active is self._mongo else "memory"

    def is_banned(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self._bans

    def is_authorized(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self._authorized

    def ban_info(self, user_id: int) -> BanEntry | None:
        return self._bans.get(user_id)

    async def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError("Unknown access mode")
        self._mode = mode
        await self._persist_access()
        return mode

    async def authorize(self, user_id: int) -> None:
        self._authorized.add(user_id)
        await self._persist_access()

    async def unauthorize(self, user_id: int) -> None:
        self._authorized.discard(user_id)
        await self._persist_access()

    async def ban(self, user_id: int, admin_id: int | None = None) -> BanEntry:
        info = await self._run("user_info", user_id, default=None) or {}
        entry = BanEntry(
            user_id=user_id,
            name=(info.get("name") or "")[:64],
            username=(info.get("username") or "")[:64],
            banned_at=utcnow(),
            banned_by=admin_id,
        )
        self._bans[user_id] = entry
        try:
            await self._active.save_ban(entry)
        except Exception as exc:  # noqa: BLE001 - the cached ban still applies
            self._degrade(exc)
        return entry

    async def release(self, user_id: int) -> bool:
        existed = self._bans.pop(user_id, None) is not None
        try:
            await self._active.delete_ban(user_id)
        except Exception as exc:  # noqa: BLE001 - the cached release still applies
            self._degrade(exc)
        return existed

    # -- users, presets and usage ---------------------------------------
    async def touch_user(self, user_id: int, name: str = "", username: str = "") -> None:
        if not user_id:
            return
        await self._run("touch_user", user_id, name[:64], username[:64], utcnow())

    async def user_info(self, user_id: int) -> dict | None:
        return await self._run("user_info", user_id, default=None)

    async def all_user_ids(self) -> tuple[int, ...]:
        """Everyone the bot has seen; /broadcast filters bans and adds owners."""
        return await self._run("user_ids", default=())

    # -- premium --------------------------------------------------------
    async def premium_until(self, user_id: int | None) -> datetime | None:
        if not user_id:
            return None
        return await self._run("premium_until", user_id, default=None)

    async def is_premium(self, user_id: int | None, now: datetime | None = None) -> bool:
        until = await self.premium_until(user_id)
        return bool(until and until > (now or utcnow()))

    async def grant_premium(self, user_id: int, days: int) -> datetime:
        """Extend premium from now (or the current expiry) and return the new end."""
        now = utcnow()
        current = await self.premium_until(user_id)
        until = max(now, current) if current else now
        until += timedelta(days=max(1, min(3650, days)))
        await self._run("set_premium", user_id, until, now)
        return until

    async def revoke_premium(self, user_id: int) -> bool:
        return bool(await self._run("clear_premium", user_id, utcnow(), default=False))

    async def presets(self, user_id: int | None) -> tuple[str, ...]:
        if not user_id:
            return ()
        codes = await self._run("get_presets", user_id, default=[]) or []
        return tuple(code for code in codes if isinstance(code, str))

    async def set_presets(self, user_id: int, codes: list[str]) -> None:
        await self._run("set_presets", user_id, list(codes), utcnow())

    async def record_translation(self, *, user_id: int, name: str, username: str, language_code: str,
                                 language_name: str, cue_count: int, filename: str,
                                 size: int | None = None, when: datetime | None = None) -> None:
        await self._run("record_translation", {
            "user_id": user_id,
            "name": (name or "")[:64],
            "username": (username or "")[:64],
            "language_code": language_code[:32],
            "language_name": language_name[:80],
            "cue_count": int(cue_count),
            "filename": (filename or "")[:200],
            "size": int(size) if size is not None else None,
            "created_at": when or utcnow(),
        })

    async def stats(self) -> Stats:
        now = utcnow()
        storage_used, storage_total = await self._run("storage", default=(None, None)) or (None, None)
        return Stats(
            users_total=await self._run("total_users", default=0) or 0,
            users_active_7d=await self._run("active_users", now - timedelta(days=7), default=0) or 0,
            banned=len(self._bans),
            authorized=len(self._authorized),
            mode=self._mode,
            translations_total=await self._run("count_translations", None, None, None, default=0) or 0,
            translations_day=await self._run("count_translations", period_start("day", now), None, None,
                                             default=0) or 0,
            translations_week=await self._run("count_translations", period_start("week", now), None, None,
                                              default=0) or 0,
            translations_month=await self._run("count_translations", period_start("month", now), None, None,
                                               default=0) or 0,
            translations_year=await self._run("count_translations", period_start("year", now), None, None,
                                              default=0) or 0,
            storage_used=storage_used,
            storage_total=storage_total,
            backend=self.backend,
        )

    async def user_counts(self, user_id: int) -> dict[str, int]:
        now = utcnow()
        counts = {"total": await self._run("count_translations", None, None, user_id, default=0) or 0}
        for period in PERIODS:
            counts[period] = await self._run("count_translations", period_start(period, now), None,
                                             user_id, default=0) or 0
        return counts

    async def leaderboard(self, period: str, limit: int = 10) -> list[LeaderEntry]:
        rows = await self._run("group_counts", period_start(period), default=[]) or []
        rows.sort(key=lambda row: (-row["count"], row["user_id"]))
        top = rows[:limit]
        if not top:
            return []
        info = await self._run("users_info", [row["user_id"] for row in top], default={}) or {}
        return [
            LeaderEntry(
                user_id=row["user_id"],
                name=(info.get(row["user_id"]) or {}).get("name") or row.get("name") or "",
                username=(info.get(row["user_id"]) or {}).get("username") or row.get("username") or "",
                count=row["count"],
            )
            for row in top
        ]

    async def rank(self, period: str, user_id: int) -> tuple[int | None, int]:
        counts = await self._run("count_translations", period_start(period), None, user_id, default=0) or 0
        if not counts:
            return None, 0
        rows = await self._run("group_counts", period_start(period), default=[]) or []
        above = sum(1 for row in rows if row["count"] > counts)
        return above + 1, counts
