"""Shared lightweight fakes for the menu, admin and access-control tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.languages import LanguageCatalog
from subtranslate.store import Database


def user(user_id=42, name="Test User", username="testuser", chat_type="private"):
    return SimpleNamespace(
        id=user_id,
        full_name=name,
        username=username,
        type=chat_type,
    )


class FakeBot:
    """Records Telegram calls without any network access."""

    def __init__(self):
        self.messages = []
        self.documents = []
        self.next_id = 500
        self.fail_chat_ids = set()
        self.files = {}

    async def get_file(self, file_id=None, **kwargs):
        payload = self.files.get(file_id, b"")

        async def download_as_bytearray(*args, **kwargs):
            return bytearray(payload)

        return SimpleNamespace(file_id=file_id, download_as_bytearray=download_as_bytearray)

    async def send_message(self, chat_id=None, text="", **kwargs):
        if chat_id in self.fail_chat_ids:
            from telegram.error import TelegramError

            raise TelegramError("blocked")
        self.next_id += 1
        record = {"chat_id": chat_id, "text": text, **kwargs}
        self.messages.append(record)
        return SimpleNamespace(message_id=self.next_id, text=text)

    async def send_document(self, chat_id=None, document=None, caption="", **kwargs):
        if chat_id in self.fail_chat_ids:
            from telegram.error import TelegramError

            raise TelegramError("blocked")
        self.next_id += 1
        self.documents.append({"chat_id": chat_id, "document": document, "caption": caption, **kwargs})
        return SimpleNamespace(message_id=self.next_id)

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        self.messages.append({"chat_id": chat_id, "message_id": message_id, "deleted": True})
        return True

    async def edit_message_text(self, chat_id=None, message_id=None, text="", **kwargs):
        self.next_id += 1
        self.messages.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        return True


class FakeMessage:
    def __init__(self, chat_id=42, message_id=100, text=""):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = message_id
        self.text = text
        self.reply_to_message = None
        self.document = None
        self.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=message_id + 1))
        self.reply_document = AsyncMock(return_value=SimpleNamespace(message_id=message_id + 2))


class FakeQuery:
    def __init__(self, data, user_id=42, message=None):
        self.data = data
        self.from_user = user(user_id)
        self.message = message or FakeMessage()
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text=None, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup, **kwargs})
        return True


def make_update(*, user_id=42, name="Test User", username="testuser", text="", chat_type="private",
                document=None):
    message = FakeMessage(chat_id=user_id, text=text)
    message.document = document
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        effective_user=user(user_id, name, username, chat_type),
        callback_query=None,
    )


def make_query_update(data, *, user_id=42, name="Test User", username="testuser"):
    query = FakeQuery(data, user_id)
    return SimpleNamespace(
        effective_message=query.message,
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        effective_user=user(user_id, name, username),
        callback_query=query,
    ), query


def make_service(*, owner=1, allowed=None, default_mode=None, log_channel=None):
    settings = Settings(
        "12345:TESTTOKEN",
        "https://example.com",
        allowed_user_ids=frozenset(allowed) if allowed else None,
        owner_ids=frozenset({owner}) if owner else frozenset(),
        log_channel_id=log_channel,
    )
    store = Database(default_mode=default_mode or settings.default_access_mode)
    return BotService(settings, LanguageCatalog(), SimpleNamespace(), store=store)


def context(*args):
    return SimpleNamespace(args=list(args))
