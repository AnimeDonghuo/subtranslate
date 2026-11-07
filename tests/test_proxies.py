from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

from subtranslate.proxies import parse_proxy_pool
from subtranslate.website import WebsiteError, WebsiteSession


def preflight_context(*, ok=True):
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(ok=ok)),
        locator=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=1))),
    )
    return SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())


async def test_proxy_pool_checks_before_upload_rotates_and_closes_contexts(caplog):
    pool = parse_proxy_pool("http://user:secret@first.test:80,http://second.test:80")
    bad, good, next_good = preflight_context(ok=False), preflight_context(), preflight_context()
    browser = SimpleNamespace(is_connected=Mock(return_value=True), close=AsyncMock(),
                              new_context=AsyncMock(side_effect=[bad, good, next_good]))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    selected = []
    result = SimpleNamespace(possibly_incomplete=False)

    async def translate(source, code, session, progress):
        selected.append(session._proxy)
        return result

    translator = SimpleNamespace(chromium_path=None, website_proxies=pool, _proxy_cursor=0,
                                 site_url="https://site.example/", _translate_in_session=translate)
    session = WebsiteSession(translator, playwright)
    assert await session.translate(object(), "es") is result
    assert await session.translate(object(), "es") is result
    assert selected == [pool[1], pool[0]]
    assert browser.new_context.await_args_list[0].kwargs["proxy"] == pool[0].playwright_options()
    for context in (bad, good, next_good):
        context.close.assert_awaited_once()
    assert browser.close.await_count == 2
    assert "secret" not in caplog.text and "first.test" not in caplog.text


async def test_all_bad_proxies_fail_closed_without_upload():
    pool = parse_proxy_pool("http://a.test:80,http://b.test:80")
    browser = SimpleNamespace(is_connected=Mock(return_value=True), close=AsyncMock(),
                              new_context=AsyncMock(side_effect=PlaywrightError("private credentials")))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    translate = AsyncMock()
    translator = SimpleNamespace(chromium_path=None, website_proxies=pool, _proxy_cursor=0,
                                 site_url="https://site.example/", _translate_in_session=translate)
    session = WebsiteSession(translator, playwright)
    with pytest.raises(WebsiteError, match="None of the configured proxies"):
        await session.translate(object(), "es")
    translate.assert_not_awaited()
    browser.close.assert_awaited_once()


async def test_selected_vless_bridge_stays_open_until_translation_finishes(monkeypatch):
    from contextlib import asynccontextmanager

    from subtranslate import website

    proxy, = parse_proxy_pool("vless://12345678-1234-4234-8234-123456789abc@remote.test:443?security=tls")
    state = []

    @asynccontextmanager
    async def bridge(candidate):
        assert candidate is proxy
        state.append("started")
        try:
            yield "socks5://127.0.0.1:12345"
        finally:
            state.append("stopped")

    monkeypatch.setattr(website, "vless_bridge", bridge)
    result = SimpleNamespace(possibly_incomplete=False)

    async def translate(source, code, session, progress):
        assert state == ["started"]
        assert session._proxy.server == "socks5://127.0.0.1:12345"
        return result

    translator = SimpleNamespace(website_proxies=(proxy,), _proxy_cursor=0, _translate_in_session=translate)
    session = WebsiteSession(translator, SimpleNamespace())
    session._probe_endpoint = AsyncMock(return_value=True)
    session._close_browser = AsyncMock()
    assert await session.translate(object(), "es") is result
    assert state == ["started", "stopped"]
    session._probe_endpoint.assert_awaited_once()
