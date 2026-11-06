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
