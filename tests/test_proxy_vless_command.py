from types import SimpleNamespace
from unittest.mock import AsyncMock

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.languages import LanguageCatalog
from subtranslate.vless import VlessProxy


async def test_command_accepts_vless_in_plain_text_without_echoing_secret():
    translator = SimpleNamespace(configure_proxies=AsyncMock(return_value=1))
    service = BotService(Settings("token", "https://example.com", allowed_user_ids=frozenset({42})),
                         LanguageCatalog(), translator)
    command_message = SimpleNamespace(reply_text=AsyncMock())
    command = SimpleNamespace(effective_message=command_message, effective_chat=SimpleNamespace(id=42, type="private"),
                              effective_user=SimpleNamespace(id=42))
    await service.proxy_command(command, SimpleNamespace(args=[]))
    secret = "12345678-1234-4234-8234-123456789abc"
    message = SimpleNamespace(text=f"vless://{secret}@example.com:443?security=tls", reply_text=AsyncMock())
    incoming = SimpleNamespace(effective_message=message, effective_chat=command.effective_chat,
                               effective_user=command.effective_user)
    await service.other_text(incoming, None)
    await service._proxy_scan_task
    proxy, = translator.configure_proxies.await_args.args[0]
    assert isinstance(proxy, VlessProxy)
    assert "1 of 1" in message.reply_text.await_args.args[0]
    assert secret not in str(message.reply_text.await_args_list)
