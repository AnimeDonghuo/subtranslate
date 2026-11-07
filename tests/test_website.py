"""Real Chromium against a local site shaped like the observed website.

These tests do not assert that the external website is currently reachable.
"""

import pytest

from subtranslate.subtitles import SubtitleError, prepare_subtitle
from subtranslate.website import LanguageUnavailable, WebsiteError, WebsiteTranslator


@pytest.fixture
def source():
    return prepare_subtitle(b"10\n00:00:01,000 --> 00:00:02,000\nHello world\n\n")


def translator(url, chromium_path):
    return WebsiteTranslator(chromium_path, site_url=url, stage_timeout_ms=5_000,
                             translation_timeout_ms=1_500, change_wait_ms=800,
                             retry_change_wait_ms=1_200, settle_ms=1_800, settle_timeout_ms=5_000)


async def test_upload_select_wait_for_late_text_and_download(mock_site, chromium_path, source):
    result = await translator(mock_site, chromium_path).translate(source, "es")
    assert b"Hola mundo" in result.content
    assert b"00:00:01,000 --> 00:00:02,000" in result.content
    assert result.content.startswith(b"10\n")
    assert not result.unchanged


async def test_output_comes_from_download_button_not_preview(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=download-different", chromium_path).translate(source, "es")
    assert result.content == (
        b"10\n00:00:01,000 --> 00:00:02,000\nText from the downloaded file\n\n"
    )
    assert b"Hola mundo" not in result.content  # The mock's preview text.


async def test_batch_reuses_browser_isolates_contexts_and_recycles_it(mock_site, chromium_path, source):
    browser = translator(mock_site, chromium_path)
    stages = []
    async with browser.session() as session:
        session.max_browser_uses = 2
        first = await session.translate(source, "es", on_progress=stages.append)
        first_browser = session._browser
        assert first.cue_count == 1 and first_browser.is_connected()
        assert not first_browser.contexts
        second = await session.translate(source, "hi")
        assert second.cue_count == 1 and session._browser is first_browser
        assert not first_browser.contexts
        third = await session.translate(source, "es")
        second_browser = session._browser
        assert third.cue_count == 1 and second_browser is not first_browser
        assert not first_browser.is_connected() and not second_browser.contexts
    assert not second_browser.is_connected()
    assert stages == [
        "starting the browser", "opening the subtitle site", "uploading and parsing the subtitle",
        "selecting the target language", "translating", "waiting for translation to settle",
        "downloading", "validating the downloaded file",
    ]


async def test_session_restarts_a_disconnected_browser():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from subtranslate.website import WebsiteSession

    old = SimpleNamespace(is_connected=Mock(return_value=False), close=AsyncMock())
    new = SimpleNamespace(is_connected=Mock(return_value=True), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=[old, new])))
    session = WebsiteSession(SimpleNamespace(chromium_path=None), playwright)
    assert await session.browser() is old
    assert await session.browser() is new
    old.close.assert_awaited_once()
    assert playwright.chromium.launch.await_count == 2
    await session._close_browser()
    new.close.assert_awaited_once()


@pytest.mark.parametrize("source_text", [
    "{25}{50}Hello world\n{51}{75}Thank you\n",
    "{1}{1}23.976\n{25}{50}Hello world\n{51}{75}Thank you\n",
    ("[INFORMATION]\n[TITLE]Sample\n[END INFORMATION]\n[SUBTITLE]\n"
     "00:00:01.00,00:00:02.00\nHello world\n\n00:00:03.00,00:00:04.00\nThank you\n"),
])
async def test_sub_variants_use_srt_bridge_and_keep_original_timing(mock_site, chromium_path, source_text):
    source = prepare_subtitle(source_text.encode(), "movie.sub")
    result = await translator(mock_site, chromium_path).translate(source, "es")
    assert result.cue_count == 2 and not result.unchanged
    assert result.content.count(b"Hola mundo") == 2
    if source_text.startswith("{"):
        assert b"{25}{50}Hola mundo" in result.content
        assert b"{51}{75}Hola mundo" in result.content
    else:
        assert b"[TITLE]Sample" in result.content
        assert b"00:00:01.00,00:00:02.00" in result.content
        assert b"00:00:03.00,00:00:04.00" in result.content


async def test_site_upload_is_retried_once_if_its_table_never_renders(mock_site, chromium_path, source, caplog):
    browser = translator(mock_site + "?mode=retryupload", chromium_path)
    browser.stage_timeout_ms = 1500
    result = await browser.translate(source, "es")
    assert b"Hola mundo" in result.content
    assert "displayed 0 of 1 expected cues" in caplog.text
    assert "Hello world" not in caplog.text


async def test_upload_timeout_after_retry_is_reported_without_private_text(mock_site, chromium_path, source, caplog):
    browser = translator(mock_site + "?mode=noupload", chromium_path)
    browser.stage_timeout_ms = 1500
    with pytest.raises(WebsiteError, match="timed out"):
        await browser.translate(source, "es")
    # Two same-context upload attempts, then the whole early flow once in a
    # fresh browser; still bounded if the site never renders a table.
    assert caplog.text.count("displayed 0 of 1 expected cues") == 4
    assert "error=TimeoutError, category=timeout" in caplog.text
    assert "Hello world" not in caplog.text


async def test_upload_uses_a_random_name_instead_of_the_users_filename(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=randomname", chromium_path).translate(source, "es")
    assert b"Hola mundo" in result.content


async def test_success_message_without_loader_is_accepted(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=successonly", chromium_path).translate(source, "hi")
    assert b"Hola mundo" in result.content


async def test_same_text_warns_instead_of_being_rejected(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=unchanged", chromium_path).translate(source, "es")
    assert result.unchanged


async def test_hidden_loader_is_not_a_false_success(mock_site, chromium_path, source, caplog):
    with pytest.raises(WebsiteError, match="timed out"):
        await translator(mock_site + "?mode=nosignal", chromium_path).translate(source, "es")
    assert "Website completion state" in caplog.text
    assert "'loaderSeen': False" in caplog.text
    assert "Hello world" not in caplog.text


async def test_rejects_unavailable_language_and_bad_timing(mock_site, chromium_path, source):
    with pytest.raises(LanguageUnavailable):
        await translator(mock_site, chromium_path).translate(source, "bn")
    with pytest.raises(SubtitleError, match="timestamps"):
        await translator(mock_site + "?mode=badtime", chromium_path).translate(source, "es")


async def test_popup_can_be_closed_before_the_table_is_rendered(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=gatepopup", chromium_path).translate(source, "es")
    assert b"Hola mundo" in result.content


async def test_retry_when_the_first_click_signals_success_without_changing_text(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=retry", chromium_path).translate(source, "es")
    assert b"Hola mundo" in result.content
    assert not result.unchanged


async def test_change_after_first_thirty_cues_does_not_trigger_a_retry(mock_site, chromium_path):
    source = prepare_subtitle("".join(
        f"{i}\n00:00:01,000 --> 00:00:02,000\nName {i}\n\n" for i in range(1, 32)
    ).encode())
    result = await translator(mock_site + "?mode=lateronly", chromium_path).translate(source, "es")
    assert b"Name 1" in result.content
    assert b"Hola mundo" in result.content
    assert b"Unexpected extra click" not in result.content


async def test_waits_for_later_translation_changes_to_settle(mock_site, chromium_path, source):
    result = await translator(mock_site + "?mode=bursts", chromium_path).translate(source, "es")
    assert b"Final translation" in result.content


async def test_does_not_download_while_loader_is_still_visible(mock_site, chromium_path, source):
    with pytest.raises(WebsiteError, match="finish settling"):
        await translator(mock_site + "?mode=stuckloader", chromium_path).translate(source, "es")


async def test_waits_until_all_uploaded_cues_are_rendered(mock_site, chromium_path):
    source = prepare_subtitle("".join(
        f"{i}\n00:00:01,000 --> 00:00:02,000\nOriginal {i}\n\n" for i in range(1, 4)
    ).encode())
    result = await translator(mock_site + "?mode=progressive", chromium_path).translate(source, "es")
    assert result.cue_count == 3
    assert result.content.count(b"Hola mundo") == 3


async def test_rejects_when_site_parses_extra_cues(mock_site, chromium_path, source):
    with pytest.raises(WebsiteError, match="different number of cues"):
        await translator(mock_site + "?mode=extracue", chromium_path).translate(source, "es")


def _large_chinese_srt(count: int):
    # Synthetic, non-private text; the mock is not a translation engine.
    return prepare_subtitle("".join(
        f"{i}\n00:00:01,000 --> 00:00:02,000\n这是一个简单的字幕测试句子 {i}\n\n"
        for i in range(1, count + 1)
    ).encode())


async def test_large_srt_uses_all_downloaded_parts_not_preview(mock_site, chromium_path):
    import srt

    source = _large_chinese_srt(405)
    stages = []
    browser = translator(mock_site + "?mode=partial250-download-different", chromium_path)
    result = await browser.translate(source, "en", on_progress=stages.append)
    cues = list(srt.parse(result.content.decode()))
    assert len(cues) == 405 and result.changed_cues == 405
    assert [cue.index for cue in cues] == list(range(1, 406))
    assert all(cue.content == "Text from the downloaded file" for cue in cues)
    assert "Hola mundo" not in result.content.decode()  # Website preview differs.
    assert [stage for stage in stages if stage.startswith("part:")] == ["part:1/3", "part:2/3", "part:3/3"]
    assert [(cue.start, cue.end) for cue in cues] == [(cue.start, cue.end) for cue in source.cues]


async def test_likely_partial_part_is_split_and_downloaded_again(mock_site, chromium_path, caplog):
    source = _large_chinese_srt(100)
    browser = translator(mock_site + "?mode=partial50", chromium_path)
    stages = []
    result = await browser.translate(source, "en", on_progress=stages.append)
    assert result.cue_count == 100 and result.changed_cues == 100
    assert result.content.count(b"Hola mundo") == 100
    assert "likely untranslated run" in caplog.text
    assert "字幕测试" not in caplog.text
    assert "part:1/2" in stages and "part:2/2" in stages


async def test_likely_untranslated_small_part_is_not_delivered_after_retry(mock_site, chromium_path, caplog):
    source = _large_chinese_srt(10)
    browser = translator(mock_site + "?mode=unchanged", chromium_path)
    browser.change_wait_ms = browser.retry_change_wait_ms = 150
    browser.settle_ms = 500
    with pytest.raises(WebsiteError, match="no incomplete file was sent"):
        await browser.translate(source, "en")
    assert "retrying once" in caplog.text
    assert "字幕测试" not in caplog.text


async def test_separate_jobs_recover_from_upload_error_in_a_fresh_browser(mock_site, chromium_path, source,
                                                                   monkeypatch, caplog):
    from playwright.async_api import Error as PlaywrightError

    from subtranslate import website

    original_wait = website._wait_for_uploaded_cues
    original_browser = website.WebsiteSession.browser
    waits = 0
    browsers = []

    async def flaky_wait(page, count, timeout):
        nonlocal waits
        waits += 1
        if waits == 2:  # The second job, not the first, fails during parsing.
            raise PlaywrightError("Execution context was destroyed PRIVATE_SUBTITLE PRIVATE_TOKEN")
        await original_wait(page, count, timeout)

    async def record_browser(session):
        browser = await original_browser(session)
        browsers.append(browser)
        return browser

    monkeypatch.setattr(website, "_wait_for_uploaded_cues", flaky_wait)
    monkeypatch.setattr(website.WebsiteSession, "browser", record_browser)
    browser = translator(mock_site, chromium_path)
    first = await browser.translate(source, "es")
    second = await browser.translate(source, "es")
    assert b"Hola mundo" in first.content and b"Hola mundo" in second.content
    assert waits == 3 and len(browsers) == 3
    assert browsers[1] is not browsers[2]
    assert all(not instance.is_connected() for instance in browsers)
    assert "error=Error, category=navigation" in caplog.text
    assert "PRIVATE_SUBTITLE" not in caplog.text and "PRIVATE_TOKEN" not in caplog.text
    assert "Hello world" not in caplog.text


async def test_same_script_partial_result_is_warned_not_claimed_as_complete(mock_site, chromium_path):
    source = prepare_subtitle("".join(
        f"{i}\n00:00:01,000 --> 00:00:02,000\nAn English subtitle sentence {i}\n\n"
        for i in range(1, 41)
    ).encode())
    result = await translator(mock_site + "?mode=partial10", chromium_path).translate(source, "es")
    assert result.changed_cues == 10 and result.longest_unchanged_run == 30
    assert result.possibly_incomplete


async def test_large_sub_bridge_combines_downloads_and_restores_every_frame(mock_site, chromium_path):
    raw = "{1}{1}23.976\n" + "".join(
        f"{{{i}}}{{{i + 1}}}Original subtitle {i}\n" for i in range(25, 230)
    )
    source = prepare_subtitle(raw.encode(), "movie.sub")
    result = await translator(mock_site + "?mode=partial250", chromium_path).translate(source, "es")
    output = result.content.decode()
    assert output.startswith("{1}{1}23.976\n")
    assert result.cue_count == 205 and result.changed_cues == 205
    assert output.count("Hola mundo") == 205
    assert "{25}{26}Hola mundo" in output and "{229}{230}Hola mundo" in output
    assert [(cue.start, cue.end) for cue in prepare_subtitle(result.content, "movie.sub").cues] == [
        (cue.start, cue.end) for cue in source.cues
    ]


def test_error_categories_are_allowlisted_and_do_not_include_details():
    from playwright.async_api import Error

    from subtranslate.website import _error_category

    assert _error_category(Error("Page crashed PRIVATE_TOKEN")) == "page_crashed"
    assert _error_category(Error("frame was detached PRIVATE_TOKEN")) == "frame_detached"
    assert _error_category(Error("net::ERR_CONNECTION_RESET PRIVATE_TOKEN")) == "network"
    assert _error_category(Error("unexpected PRIVATE_TOKEN")) == "other"
