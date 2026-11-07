from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from subtranslate.bot import BotService
from subtranslate.config import Settings
from subtranslate.languages import LanguageCatalog
from subtranslate.proxies import parse_proxy_pool
from subtranslate.website import WebsiteTranslator


def update(*, text=None, document=None, user=42, chat_type="private"):
    message = SimpleNamespace(text=text, document=document, reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=user, type=chat_type),
                           effective_user=SimpleNamespace(id=user))


def service(allowed=frozenset({42})):
    translator = SimpleNamespace(configure_proxies=AsyncMock(return_value=1))
    bot = BotService(Settings("token", "https://example.com", allowed_user_ids=allowed), LanguageCatalog(), translator)
    return bot


async def test_proxy_command_text_and_direct():
    bot = service()
    command = update()
    await bot.proxy_command(command, SimpleNamespace(args=[]))
    incoming = update(text="http://user:secret@one.test:8080\nsocks5://two.test:1080")
    await bot.other_text(incoming, None)
    await bot._proxy_scan_task
    proxies = bot.translator.configure_proxies.await_args.args[0]
    assert len(proxies) == 2 and proxies[0].password == "secret"
    assert "1 of 2" in incoming.effective_message.reply_text.await_args.args[0]
    assert "secret" not in str(incoming.effective_message.reply_text.await_args_list)
    direct = update()
    await bot.proxy_command(direct, SimpleNamespace(args=["direct"]))
    assert bot.translator.configure_proxies.await_args.args[0] == ()
    assert "Direct server connection enabled" in direct.effective_message.reply_text.await_args.args[0]


async def test_proxy_file_and_invalid_vless_leave_configuration_unchanged():
    bot = service()
    data = b"http://one.test:8080\n"
    bot.bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(data)))))
    await bot.proxy_command(update(), SimpleNamespace(args=[]))
    file_update = update(document=SimpleNamespace(file_id="id", file_size=len(data)))
    await bot.document(file_update, None)
    await bot._proxy_scan_task
    assert bot.translator.configure_proxies.await_count == 1
    await bot.proxy_command(update(), SimpleNamespace(args=[]))
    invalid = update(text="vless://private-uuid@server.example:443?security=tls")
    await bot.other_text(invalid, None)
    await bot._proxy_scan_task
    assert "No valid or supported" in invalid.effective_message.reply_text.await_args.args[0]
    assert "private-uuid" not in str(invalid.effective_message.reply_text.await_args_list)
    assert bot.translator.configure_proxies.await_count == 1


async def test_admin_required_and_no_cross_user_input():
    unrestricted = service(allowed=None)
    request = update()
    await unrestricted.proxy_command(request, SimpleNamespace(args=[]))
    assert "Set ALLOWED_USER_IDS" in request.effective_message.reply_text.await_args.args[0]
    assert not unrestricted._proxy_input_chats
    restricted = service()
    denied = update(user=43)
    await restricted.proxy_command(denied, SimpleNamespace(args=[]))
    assert not restricted._proxy_input_chats
    await restricted.proxy_command(update(), SimpleNamespace(args=[]))
    await restricted.other_text(update(user=43, text="direct"), None)
    assert restricted._proxy_input_chats == {42}
    restricted.translator.configure_proxies.assert_not_awaited()


async def test_proxy_file_limit_and_cancel():
    bot = service()
    await bot.proxy_command(update(), SimpleNamespace(args=[]))
    large = update(document=SimpleNamespace(file_size=19 * 1024 * 1024 + 1))
    await bot.document(large, None)
    assert "at most 19 MiB" in large.effective_message.reply_text.await_args.args[0]
    await bot.proxy_command(update(), SimpleNamespace(args=[]))
    await bot.proxy_command(update(), SimpleNamespace(args=["cancel"]))
    assert not bot._proxy_input_chats
    bot.translator.configure_proxies.assert_not_awaited()


async def test_checked_pool_replaces_only_with_working_and_keeps_old_on_failure():
    old = parse_proxy_pool("http://old.test:8080")
    new = parse_proxy_pool("http://a.test:80,http://b.test:80")
    translator = WebsiteTranslator(website_proxies=old)
    session = SimpleNamespace(_probe_proxy=AsyncMock(side_effect=[False, True, False, False]))

    @asynccontextmanager
    async def fake_session():
        yield session

    translator.session = fake_session
    assert await translator.configure_proxies(new) == 1
    assert translator.website_proxies == (new[1],)
    assert await translator.configure_proxies(new) == 0
    assert translator.website_proxies == (new[1],)
    assert await translator.configure_proxies(()) == 0
    assert translator.website_proxies == ()


async def test_direct_waits_for_active_browser_session():
    import asyncio

    translator = WebsiteTranslator(website_proxies=parse_proxy_pool("host.test:80"))
    await translator._browser_lock.acquire()
    task = asyncio.create_task(translator.configure_proxies(()))
    await asyncio.sleep(0)
    assert not task.done() and translator.website_proxies
    translator._browser_lock.release()
    assert await task == 0 and translator.website_proxies == ()


async def test_background_scan_reports_status_and_cancels():
    import asyncio

    bot = service()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def configure(proxies, *, on_progress=None):
        try:
            on_progress(1, 1)
            started.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    bot.translator.configure_proxies = AsyncMock(side_effect=configure)
    await bot.proxy_command(update(), SimpleNamespace(args=[]))
    message = update(text="http://a.test:80\nhttp://b.test:80\ninvalid")
    await bot.other_text(message, None)
    await asyncio.wait_for(started.wait(), 2)
    status = update()
    await bot.proxy_command(status, SimpleNamespace(args=["status"]))
    assert "1/2 checked, 1 passed; 1 invalid" in status.effective_message.reply_text.await_args.args[0]
    cancel = update()
    await bot.proxy_command(cancel, SimpleNamespace(args=["cancel"]))
    assert stopped.is_set()
    assert "scan canceled" in cancel.effective_message.reply_text.await_args.args[0]


async def test_cancelled_scan_does_not_replace_previous_pool():
    import asyncio

    old = parse_proxy_pool("http://old.test:8080")
    new = parse_proxy_pool("http://a.test:80,http://b.test:80,http://c.test:80")
    translator = WebsiteTranslator(website_proxies=old)
    started = asyncio.Event()
    closed = asyncio.Event()
    calls = 0

    async def probe(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await asyncio.Event().wait()
        return True

    @asynccontextmanager
    async def fake_session():
        try:
            yield SimpleNamespace(_probe_proxy=probe)
        finally:
            closed.set()

    translator.session = fake_session
    task = asyncio.create_task(translator.configure_proxies(new))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert translator.website_proxies == old and closed.is_set()
