"""Drive the public translatesubtitles.co website; this is not an API client."""

import asyncio
import logging
import re
import secrets
import tempfile
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .subtitles import (
    MAX_OUTPUT_BYTES,
    PreparedSubtitle,
    TranslatedSubtitle,
    combine_srt_downloads,
    obviously_untranslated_run,
    restore_subtitle_from_website,
    split_srt_for_website,
    subtitle_for_website,
    verify_and_merge,
)

logger = logging.getLogger(__name__)
SITE_URL = "https://translatesubtitles.co/"
AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "google-analytics.com", "googletagmanager.com",
    "monumetric.com", "monu.delivery", "amazon-adsystem.com",
)


class WebsiteError(Exception):
    """A user-facing failure while interacting with the third-party site."""


class LanguageUnavailable(WebsiteError):
    pass


class _RetryableWebsiteError(WebsiteError):
    """An early browser/site failure for which a fresh browser may help."""


def _error_category(exc: Exception) -> str:
    """Classify common Playwright failures without logging exception details."""
    detail = str(exc).lower()
    if "page crashed" in detail or "page crash" in detail:
        return "page_crashed"
    if (type(exc).__name__ == "TargetClosedError" or "target page, context or browser has been closed" in detail
            or "target closed" in detail):
        return "page_or_browser_closed"
    if "execution context was destroyed" in detail or "because of a navigation" in detail:
        return "navigation"
    if "frame was detached" in detail:
        return "frame_detached"
    if "net::" in detail or "connection refused" in detail or "connection reset" in detail:
        return "network"
    if "timeout" in detail or "timed out" in detail:
        return "timeout"
    return "other"


def _stage(name: str, on_progress: Callable[[str], None] | None = None) -> str:
    # Stage names are fixed strings or numeric part counts; never log subtitle text or filenames.
    logger.info("Subtitle website: %s", name)
    if on_progress is not None:
        try:
            on_progress(name)
        except Exception as exc:  # noqa: BLE001 - status updates must not break a translation
            logger.warning("Could not update translation progress (%s)", type(exc).__name__)
    return name


async def _close_vip_popup(page) -> None:
    close = page.locator(".popup-vip.notranslate a.close").first
    try:
        if await close.count() and await close.is_visible():
            await close.click(timeout=3_000)
    except PlaywrightError:
        # The popup is optional and can disappear on its own.
        pass


async def _wait_for_uploaded_cues(page, expected_count: int, timeout_ms: int) -> None:
    """Wait for every cue while dismissing interstitials that block parsing."""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    cells = page.locator("td.notranslate.target-sub")
    while True:
        await _close_vip_popup(page)
        count = await cells.count()
        if count == expected_count:
            return
        if count > expected_count:
            raise WebsiteError("The website parsed a different number of cues. Please try again.")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            # Counts only: do not log filenames, URLs or subtitle text.
            logger.warning("Subtitle website displayed %d of %d expected cues before timeout", count, expected_count)
            raise PlaywrightTimeout("The website did not display all uploaded subtitle cues")
        try:
            await cells.nth(expected_count - 1).wait_for(
                state="attached", timeout=min(1000, remaining * 1000)
            )
        except PlaywrightTimeout:
            # A VIP interstitial may be what prevents the next page loading.
            continue


async def _button(page, label: str, number: int):
    # The site renders its controls as links. The Google Translate widget also
    # has a plain "Translate" link, so prefer the exact numbered site control.
    numbered = re.compile(rf"^\s*{number}\.\s*{label}\s*$", re.IGNORECASE)
    button = page.get_by_role("button", name=numbered).first
    if await button.count():
        return button
    text = page.get_by_text(numbered).first
    if await text.count():
        return text
    # If a future version drops numbering, only accept a real button, not the
    # Google branding link. The final locator will time out if neither exists.
    plain_button = page.get_by_role("button", name=re.compile(rf"^\s*{label}\s*$", re.IGNORECASE)).first
    if await plain_button.count():
        return plain_button
    return text


async def _block_unnecessary_requests(route) -> None:
    request = route.request
    host = (urlsplit(request.url).hostname or "").lower()
    is_ad = any(host == suffix or host.endswith("." + suffix) for suffix in AD_HOSTS)
    if is_ad or request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


# The site has both a loader and a success message. The old automation examples
# wait for an already-hidden loader, which can report success immediately. Watch
# for an actual visible -> hidden transition, or a newly visible success message.
PROGRESS_WATCH = r"""() => {
    const visible = (el) => !!(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).opacity !== '0');
    const success = () => document.querySelector('h4.skiptranslate.success-msg');
    const state = window.__subtranslateWatch = {
        successInitiallyVisible: visible(success()),
        loaderSeen: false,
        loaderFinished: false,
        lastTextChange: performance.now(),
    };
    const observer = new MutationObserver((mutations) => {
        const loader = document.querySelector('#loader');
        if (visible(loader)) state.loaderSeen = true;
        else if (state.loaderSeen) state.loaderFinished = true;
        for (const mutation of mutations) {
            const el = mutation.target.nodeType === Node.ELEMENT_NODE ?
                mutation.target : mutation.target.parentElement;
            if (el && el.closest && el.closest('td.notranslate.target-sub')) {
                state.lastTextChange = performance.now();
                break; // One timestamp per mutation batch is enough, even for thousands of cues.
            }
        }
    });
    observer.observe(document.documentElement, {subtree: true, attributes: true,
        childList: true, characterData: true});
}"""

PROGRESS_FINISHED = r"""() => {
    const state = window.__subtranslateWatch;
    if (!state) return false;
    const el = document.querySelector('h4.skiptranslate.success-msg');
    const success = !!(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).opacity !== '0');
    return state.loaderFinished || (!state.successInitiallyVisible && success);
}"""

PROGRESS_DIAGNOSTICS = r"""() => {
    const visible = (el) => !!(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).opacity !== '0');
    const loader = document.querySelector('#loader');
    const success = document.querySelector('h4.skiptranslate.success-msg');
    const state = window.__subtranslateWatch;
    return {
        loaderPresent: !!loader,
        loaderVisible: visible(loader),
        successPresent: !!success,
        successVisible: visible(success),
        successInitiallyVisible: state?.successInitiallyVisible ?? null,
        loaderSeen: state?.loaderSeen ?? null,
        loaderFinished: state?.loaderFinished ?? null,
    };
}"""

CACHE_INITIAL_CUES = r"""() => {
    // Keep this only in the page: it is used to detect changes, never to
    // construct the output file. Avoid transferring all subtitle text twice.
    const cells = document.querySelectorAll('td.notranslate.target-sub');
    window.__subtranslateBefore = Array.from(cells, el => (el.textContent || '').trim());
    return cells.length;
}"""

TEXT_CHANGED = r"""() => {
    const before = window.__subtranslateBefore;
    if (!before) return false;
    const after = document.querySelectorAll('td.notranslate.target-sub');
    return before.some((text, index) => {
        const current = (after[index]?.textContent || '').trim();
        return current && current !== text;
    });
}"""

TEXT_STABLE = r"""(settleMs) => {
    const loader = document.querySelector('#loader');
    const loading = !!(loader && loader.getClientRects().length &&
        getComputedStyle(loader).visibility !== 'hidden' && getComputedStyle(loader).opacity !== '0');
    return window.__subtranslateWatch && !loading &&
        performance.now() - window.__subtranslateWatch.lastTextChange >= settleMs;
}"""


class WebsiteSession:
    """Reuse a browser for a batch, but isolate every language in a fresh context."""

    def __init__(self, translator, playwright, *, max_browser_uses: int = 10) -> None:
        self.translator = translator
        self.playwright = playwright
        self.max_browser_uses = max_browser_uses
        self._browser = None
        self._uses = 0

    async def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError as exc:
                logger.warning("Could not close the subtitle browser (%s)", type(exc).__name__)
            finally:
                self._browser = None
                self._uses = 0

    async def browser(self):
        if self._browser is not None and (not self._browser.is_connected() or self._uses >= self.max_browser_uses):
            # Recycling periodically bounds long-batch memory growth and also
            # restarts a browser that crashed between languages.
            await self._close_browser()
        if self._browser is None:
            self._browser = await self.playwright.chromium.launch(
                executable_path=self.translator.chromium_path,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        self._uses += 1
        return self._browser

    async def _translate_part(self, source: PreparedSubtitle, language_code: str,
                              on_progress: Callable[[str], None] | None) -> TranslatedSubtitle:
        # Once the site has started translating, do not blindly repeat that
        # operation. Early upload/navigation failures are safe to retry once
        # with a new browser and isolated context.
        for attempt in range(2):
            try:
                return await self.translator._translate_in_session(source, language_code, self, on_progress)
            except _RetryableWebsiteError:
                if attempt:
                    raise WebsiteError(
                        "The subtitle website could not open or upload the file after retrying. "
                        "It may have timed out or be unavailable; please try again later."
                    ) from None
                logger.info("Retrying the early website flow with a fresh browser")
                await self._close_browser()
                await asyncio.sleep(1)
        raise AssertionError("unreachable")

    async def translate(self, source: PreparedSubtitle, language_code: str, *,
                        on_progress: Callable[[str], None] | None = None) -> TranslatedSubtitle:
        # Use the site's Download button for every part. Bounded SRT uploads
        # reduce the chance of a successful-looking but partial large result.
        site_source = await asyncio.to_thread(subtitle_for_website, source)
        first_parts = await asyncio.to_thread(split_srt_for_website, site_source)
        pending = deque((part, 0) for part in first_parts)
        total = len(pending)
        completed = 0
        downloads = []
        while pending:
            part, depth = pending.popleft()
            if total > 1:
                _stage(f"part:{completed + 1}/{total}", on_progress)
            result = await self._translate_part(part, language_code, on_progress)
            suspicious = await asyncio.to_thread(obviously_untranslated_run, part, result, language_code)
            if suspicious:
                # A smaller upload can recover from sites that stop translating
                # midway. The depth limit keeps repeated retries bounded.
                smaller = []
                if part.extension == ".srt" and len(part.cues) >= 20 and depth < 4:
                    smaller = await asyncio.to_thread(
                        split_srt_for_website, part, max_cues=max(1, len(part.cues) // 2)
                    )
                if len(smaller) > 1:
                    logger.warning("Website download has a likely untranslated run (%d cues); splitting the part", suspicious)
                    total += len(smaller) - 1
                    pending.extendleft((subpart, depth + 1) for subpart in reversed(smaller))
                    await self._close_browser()
                    continue
                logger.warning("Website download has a likely untranslated run (%d cues); retrying once", suspicious)
                await self._close_browser()
                result = await self._translate_part(part, language_code, on_progress)
                if await asyncio.to_thread(obviously_untranslated_run, part, result, language_code):
                    raise WebsiteError(
                        "The website left a section in the original language after retrying, "
                        "so no incomplete file was sent. Please try again later or use a smaller subtitle."
                    )
            downloads.append(result)
            completed += 1
        if len(downloads) == 1:
            site_result = downloads[0]  # Keep the site's actual bytes for a single SRT download.
        else:
            site_result = await asyncio.to_thread(combine_srt_downloads, site_source, downloads)
        return await asyncio.to_thread(restore_subtitle_from_website, source, site_result)


class WebsiteTranslator:
    def __init__(
        self,
        chromium_path: str | None = None,
        *,
        site_url: str = SITE_URL,
        stage_timeout_ms: int = 90_000,
        translation_timeout_ms: int = 900_000,
        change_wait_ms: int = 8_000,
        retry_change_wait_ms: int = 30_000,
        settle_ms: int = 5_000,
        settle_timeout_ms: int = 60_000,
    ) -> None:
        self.chromium_path = chromium_path
        # site_url is injectable for local integration tests, not user input.
        self.site_url = site_url
        self.stage_timeout_ms = stage_timeout_ms
        self.translation_timeout_ms = translation_timeout_ms
        self.change_wait_ms = change_wait_ms
        self.retry_change_wait_ms = retry_change_wait_ms
        self.settle_ms = settle_ms
        self.settle_timeout_ms = settle_timeout_ms

    @asynccontextmanager
    async def session(self):
        """Open one Playwright session for one or several sequential languages."""
        try:
            async with async_playwright() as playwright:
                session = WebsiteSession(self, playwright)
                try:
                    yield session
                finally:
                    await session._close_browser()
        except (PlaywrightError, OSError) as exc:
            logger.warning("Could not open or close the subtitle browser (%s)", type(exc).__name__)
            raise WebsiteError("The browser could not process the subtitle file. Please try again later.") from exc

    async def translate(self, source: PreparedSubtitle, language_code: str, *,
                        on_progress: Callable[[str], None] | None = None) -> TranslatedSubtitle:
        async with self.session() as session:
            return await session.translate(source, language_code, on_progress=on_progress)

    async def _translate_in_session(self, source: PreparedSubtitle, language_code: str, session: WebsiteSession,
                                    on_progress: Callable[[str], None] | None) -> TranslatedSubtitle:
        stage = _stage("starting the browser", on_progress)
        browser = page = None
        browser_connected = page_closed = None
        try:
            site_source = subtitle_for_website(source)
            with tempfile.TemporaryDirectory(prefix="subtranslate-") as temp_dir:
                # Avoid predictable upload names if the third-party site stores
                # uploads by filename. Never send the user's original filename.
                input_path = Path(temp_dir) / f"subtitle-{secrets.token_hex(12)}{site_source.extension}"
                output_path = Path(temp_dir) / f"download{site_source.extension}"
                input_path.write_bytes(site_source.utf8)
                browser = await session.browser()
                context = await browser.new_context(
                    accept_downloads=True, locale="en-US", service_workers="block"
                )
                try:
                    await context.route("**/*", _block_unnecessary_requests)
                    page = await context.new_page()
                    page.set_default_timeout(self.stage_timeout_ms)

                    stage = _stage("opening the subtitle site", on_progress)
                    await page.goto(self.site_url, wait_until="domcontentloaded", timeout=self.stage_timeout_ms)
                    stage = _stage("uploading and parsing the subtitle", on_progress)
                    await page.locator('input[type="file"]').first.set_input_files(str(input_path))
                    # The uploaded file is parsed on the website's next page.
                    try:
                        await _wait_for_uploaded_cues(page, len(site_source.cues), self.stage_timeout_ms)
                    except PlaywrightTimeout:
                        # Transient uploads sometimes never render their table.
                        # Retry only before translation has started.
                        logger.info("Retrying the subtitle upload once")
                        await page.goto(self.site_url, wait_until="domcontentloaded", timeout=self.stage_timeout_ms)
                        await page.locator('input[type="file"]').first.set_input_files(str(input_path))
                        await _wait_for_uploaded_cues(page, len(site_source.cues), self.stage_timeout_ms)
                    selector = page.locator("select.goog-te-combo").first
                    await selector.locator("option").nth(1).wait_for(state="attached", timeout=self.stage_timeout_ms)
                    await _close_vip_popup(page)
                    # Track changes inside the page, not by copying its preview
                    # into Python. Names or numbers can remain unchanged, so
                    # compare every cue when deciding whether to retry.
                    initial_count = await page.evaluate(CACHE_INITIAL_CUES)
                    if initial_count != len(site_source.cues):
                        raise WebsiteError("The website parsed a different number of cues. Please try again.")

                    stage = _stage("selecting the target language", on_progress)
                    values = await selector.locator("option").evaluate_all("els => els.map(el => el.value)")
                    if language_code not in values:
                        raise LanguageUnavailable(
                            "That language is no longer available on the subtitle site. Please choose another."
                        )
                    await selector.select_option(value=language_code)
                    await _close_vip_popup(page)

                    stage = _stage("translating", on_progress)
                    # Install before clicking so even a very fast loader is observed.
                    await page.evaluate(PROGRESS_WATCH)
                    await _close_vip_popup(page)
                    translate_button = await _button(page, "Translate", 1)
                    await translate_button.click()
                    try:
                        await page.wait_for_function(
                            PROGRESS_FINISHED, timeout=self.translation_timeout_ms, polling=250
                        )
                    except PlaywrightTimeout:
                        try:
                            # Only booleans about the UI, never subtitle content.
                            state = await page.evaluate(PROGRESS_DIAGNOSTICS)
                            logger.warning("Website completion state: %s", state)
                        except PlaywrightError:
                            logger.info("Website completion state could not be inspected")
                        raise
                    stage = _stage("waiting for translation to settle", on_progress)
                    # Some site versions signal success early or need a second
                    # click for very short files. Retry once only if no cue has
                    # changed; same-language translations may also be unchanged.
                    try:
                        await page.wait_for_function(
                            TEXT_CHANGED, timeout=self.change_wait_ms, polling=500
                        )
                    except PlaywrightTimeout:
                        await _close_vip_popup(page)
                        try:
                            await translate_button.click(timeout=3_000)
                        except PlaywrightError:
                            # The retry is optional; some site versions disable
                            # the button after a legitimate same-language result.
                            logger.info("Could not retry the website's translation button")
                        else:
                            try:
                                await page.wait_for_function(
                                    TEXT_CHANGED, timeout=self.retry_change_wait_ms, polling=500
                                )
                            except PlaywrightTimeout:
                                pass
                    try:
                        await page.wait_for_function(
                            TEXT_STABLE, arg=self.settle_ms, timeout=self.settle_timeout_ms, polling=500
                        )
                    except PlaywrightTimeout:
                        raise WebsiteError("The website's translation did not finish settling. Please try again.")

                    stage = _stage("downloading", on_progress)
                    await _close_vip_popup(page)
                    download_button = await _button(page, "Download", 2)
                    async with page.expect_download(timeout=self.stage_timeout_ms) as download_info:
                        await download_button.click()
                    download = await download_info.value
                    await download.save_as(str(output_path))
                    if output_path.stat().st_size > MAX_OUTPUT_BYTES:
                        raise WebsiteError("The site returned an unexpectedly large file. Please try again.")
                    downloaded = output_path.read_bytes()
                finally:
                    # Capture state before closing the context; afterward a
                    # healthy page would always appear closed. No URLs, file
                    # names, subtitle contents or exception messages are logged.
                    browser_connected = browser.is_connected()
                    page_closed = page.is_closed() if page is not None else None
                    try:
                        await context.close()
                    except PlaywrightError as exc:
                        logger.warning("Could not close the website context (%s)", type(exc).__name__)
                # Refuse missing cues or changed times rather than silently
                # sending broken subtitles to the user. SUB inputs use an SRT
                # bridge; restore their original frames/timing afterward.
                stage = _stage("validating the downloaded file", on_progress)
                verified = await asyncio.to_thread(verify_and_merge, site_source, downloaded)
                return await asyncio.to_thread(restore_subtitle_from_website, source, verified)
        except LanguageUnavailable:
            raise
        except WebsiteError:
            raise
        except (PlaywrightTimeout, PlaywrightError) as exc:
            # The message and traceback may contain private subtitle content,
            # paths, or capability URLs. Log only an allowlisted category,
            # exception class, stage and browser/page state.
            category = "timeout" if isinstance(exc, PlaywrightTimeout) else _error_category(exc)
            if browser_connected is None and browser is not None:
                browser_connected = browser.is_connected()
            logger.warning(
                "Subtitle website interaction failed while %s (error=%s, category=%s, browser_connected=%s, page_closed=%s)",
                stage, type(exc).__name__, category, browser_connected, page_closed,
            )
            if stage in {"starting the browser", "opening the subtitle site", "uploading and parsing the subtitle"}:
                raise _RetryableWebsiteError("The early website flow failed.") from None
            if isinstance(exc, PlaywrightTimeout):
                raise WebsiteError("The subtitle website timed out. Please try again later or use a smaller file.") from None
            raise WebsiteError("Could not complete the subtitle website flow. It may be unavailable or have changed.") from None
        except OSError as exc:
            logger.warning("A local file or browser error occurred while %s (error=%s)", stage, type(exc).__name__)
            raise WebsiteError("The browser could not process the subtitle file. Please try again later.") from None
