"""Drive the public translatesubtitles.co website; this is not an API client."""

import asyncio
import errno
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .fileio import run_scan
from .large_srt import FileSubtitle, convert_to_utf8, verify_file_srt
from .proxies import BrowserProxy, VlessProxy
from .subtitles import (
    MAX_OUTPUT_BYTES,
    PreparedSubtitle,
    TranslatedSubtitle,
    obviously_untranslated_run,
    restore_subtitle_from_website,
    subtitle_for_website,
    verify_and_merge,
)
from .vless import ProxyBridgeError, vless_bridge

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
    """A browser/site failure before the subtitle upload started."""


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


def _retain_download(source: Path, destination: Path, stop=None) -> None:
    """Keep Playwright's actual downloaded file without a same-disk copy."""
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Different filesystems cannot rename; copy in bounded chunks and
        # let cancellation stop the copy before temporary files are removed.
        with source.open("rb") as original, destination.open("wb") as target:
            while chunk := original.read(1024 * 1024):
                if stop is not None and stop.is_set():
                    raise InterruptedError("Download copy canceled")
                target.write(chunk)


def _stage(name: str, on_progress: Callable[[str], None] | None = None) -> str:
    # Stage names are fixed strings; never log subtitle text or filenames.
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    last_count = 0
    cells = page.locator("td.notranslate.target-sub")
    while True:
        await _close_vip_popup(page)
        count = await cells.count()
        if count > last_count:
            # Parsing can be incremental for a large file. Bound a stall, not
            # the total duration while more cues are actively appearing.
            deadline = loop.time() + timeout_ms / 1000
            last_count = count
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
    const visible = (el) => {
        if (!el || !el.getClientRects().length) return false;
        for (let node = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
        }
        return true;
    };
    const busy = () => Array.from(document.querySelectorAll('#loader, .goog-te-spinner-pos')).some(visible);
    const success = () => visible(document.querySelector('h4.skiptranslate.success-msg'));
    let lastBusy = busy(), lastSuccess = success();
    const state = window.__subtranslateWatch = {
        successInitiallyVisible: lastSuccess,
        loaderSeen: lastBusy,
        loaderFinished: false,
        lastTextChange: performance.now(),
        lastIndicatorChange: performance.now(),
    };
    // Polling calls this too: CSS animations can change visibility without
    // a DOM mutation, so the quiet period must start when they actually stop.
    state.sampleIndicators = () => {
        const loading = busy(), succeeded = success();
        if (loading !== lastBusy || succeeded !== lastSuccess) {
            state.lastIndicatorChange = performance.now();
            lastBusy = loading;
            lastSuccess = succeeded;
        }
        if (loading) {
            state.loaderSeen = true;
            state.loaderFinished = false;
        } else if (state.loaderSeen) {
            state.loaderFinished = true;
        }
        return {loading, succeeded};
    };
    const observer = new MutationObserver((mutations) => {
        state.sampleIndicators();
        for (const mutation of mutations) {
            const el = mutation.target.nodeType === Node.ELEMENT_NODE ?
                mutation.target : mutation.target.parentElement;
            if (el && el.closest && el.closest('td.notranslate.target-sub')) {
                state.lastTextChange = performance.now();
                break;
            }
        }
    });
    observer.observe(document.documentElement, {subtree: true, attributes: true,
        childList: true, characterData: true});
}"""

PROGRESS_FINISHED = r"""() => {
    const state = window.__subtranslateWatch;
    if (!state) return null;
    const {loading, succeeded} = state.sampleIndicators();
    // A success message is not enough while a translation animation is
    // still visible. If no loader is present, accept a new success message.
    return {
        ready: !loading && (state.loaderFinished || (!state.successInitiallyVisible && succeeded)),
        textChange: state.lastTextChange,
        indicatorChange: state.lastIndicatorChange,
    };
}"""

PROGRESS_DIAGNOSTICS = r"""() => {
    const visible = (el) => {
        if (!el || !el.getClientRects().length) return false;
        for (let node = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
        }
        return true;
    };
    const loader = document.querySelector('#loader');
    const spinner = document.querySelector('.goog-te-spinner-pos');
    const success = document.querySelector('h4.skiptranslate.success-msg');
    const state = window.__subtranslateWatch;
    return {
        loaderPresent: !!loader,
        loaderVisible: visible(loader),
        spinnerVisible: visible(spinner),
        successPresent: !!success,
        successVisible: visible(success),
        successInitiallyVisible: state?.successInitiallyVisible ?? null,
        loaderSeen: state?.loaderSeen ?? null,
        loaderFinished: state?.loaderFinished ?? null,
    };
}"""

CACHE_INITIAL_CUES = r"""() => {
    // Keep only lightweight fingerprints in the page. The preview is never
    // used to construct the output; this merely detects any changed cue.
    const hash = window.__subtranslateHash = (text) => {
        let value = 2166136261;
        for (let i = 0; i < text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 16777619);
        return value >>> 0;
    };
    const cells = document.querySelectorAll('td.notranslate.target-sub');
    const before = window.__subtranslateBefore = new Uint32Array(cells.length * 2);
    cells.forEach((el, index) => {
        const text = (el.textContent || '').trim();
        before[index * 2] = text.length;
        before[index * 2 + 1] = hash(text);
    });
    return cells.length;
}"""

TEXT_CHANGED = r"""() => {
    const before = window.__subtranslateBefore, hash = window.__subtranslateHash;
    if (!before || !hash) return false;
    const after = document.querySelectorAll('td.notranslate.target-sub');
    for (let index = 0; index < after.length; index++) {
        const text = (after[index]?.textContent || '').trim();
        if (text && (text.length !== before[index * 2] || hash(text) !== before[index * 2 + 1])) return true;
    }
    return false;
}"""

TEXT_CHANGE_COUNTS = r"""() => {
    const before = window.__subtranslateBefore, hash = window.__subtranslateHash;
    const cells = document.querySelectorAll('td.notranslate.target-sub');
    if (!before || !hash) return {count: cells.length, changed: 0, longestUnchanged: 0};
    let changed = 0, unchangedRun = 0, longestUnchanged = 0;
    cells.forEach((cell, index) => {
        const text = (cell.textContent || '').trim();
        if (text && (text.length !== before[index * 2] || hash(text) !== before[index * 2 + 1])) {
            changed++;
            unchangedRun = 0;
        } else if (text) {
            longestUnchanged = Math.max(longestUnchanged, ++unchangedRun);
        } else {
            unchangedRun = 0;
        }
    });
    return {count: cells.length, changed, longestUnchanged};
}"""

TEXT_STABLE = r"""(settleMs) => {
    const state = window.__subtranslateWatch;
    if (!state) return null;
    const {loading, succeeded} = state.sampleIndicators();
    const finished = state.loaderFinished || (!state.successInitiallyVisible && succeeded);
    // Wait for both indicators and all table text to stay quiet. This avoids
    // downloading during a late or temporarily hidden translation animation.
    return {
        ready: finished && !loading && performance.now() -
            Math.max(state.lastTextChange, state.lastIndicatorChange) >= settleMs,
        textChange: state.lastTextChange,
        indicatorChange: state.lastIndicatorChange,
    };
}"""


async def _wait_for_site_progress(page, expression: str, idle_timeout_ms: int, *, arg=None,
                                  interval: float = 0.25) -> None:
    """Bound inactivity, not the total time while the website keeps updating."""
    loop = asyncio.get_running_loop()
    idle_seconds = idle_timeout_ms / 1000
    deadline = loop.time() + idle_seconds
    previous = None
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise PlaywrightTimeout("The subtitle website stopped making progress")
        try:
            async with asyncio.timeout(remaining):
                state = await page.evaluate(expression, arg) if arg is not None else await page.evaluate(expression)
        except TimeoutError:
            # This also covers an unresponsive browser's JS execution thread.
            raise PlaywrightTimeout("The subtitle website stopped making progress") from None
        if state is None:
            raise PlaywrightError("The subtitle website's progress state disappeared")
        marker = (state["textChange"], state["indicatorChange"])
        if previous is not None and marker != previous:
            deadline = loop.time() + idle_seconds
        previous = marker
        if state["ready"]:
            return
        await asyncio.sleep(min(interval, max(0, deadline - loop.time())))


class WebsiteSession:
    """Keep batch control separate from each translation's disposable browser."""

    def __init__(self, translator, playwright) -> None:
        self.translator = translator
        self.playwright = playwright
        self._browser = None
        self._proxy: BrowserProxy | None = None

    async def _proxy_endpoint(self, proxy: BrowserProxy | VlessProxy, stack: AsyncExitStack) -> BrowserProxy:
        if isinstance(proxy, VlessProxy):
            server = await stack.enter_async_context(vless_bridge(proxy))
            return BrowserProxy(server)
        return proxy

    async def _probe_endpoint(self, endpoint: BrowserProxy) -> bool:
        context = None
        try:
            async with asyncio.timeout(20):
                browser = await self.browser()
                context = await browser.new_context(proxy=endpoint.playwright_options(), service_workers="block")
                await context.route("**/*", _block_unnecessary_requests)
                page = await context.new_page()
                response = await page.goto(self.translator.site_url, wait_until="domcontentloaded", timeout=18_000)
                return bool(response and response.ok and await page.locator('input[type="file"]').count())
        except (PlaywrightError, TimeoutError):
            return False
        finally:
            if context is not None:
                try:
                    await context.close()
                except PlaywrightError:
                    logger.warning("Could not close proxy preflight context")

    async def _probe_proxy(self, proxy: BrowserProxy | VlessProxy) -> bool:
        """Test a candidate without uploading, closing any bridge afterward."""
        try:
            async with AsyncExitStack() as stack:
                endpoint = await self._proxy_endpoint(proxy, stack)
                return await self._probe_endpoint(endpoint)
        except ProxyBridgeError:
            return False

    async def _select_proxy(self, stack: AsyncExitStack) -> None:
        pool = self.translator.website_proxies
        if not pool:
            return
        start = self.translator._proxy_cursor % len(pool)
        # A large checked pool can later go offline. Bound each job's delay.
        attempts = min(len(pool), 10)
        for offset in range(attempts):
            index = (start + offset) % len(pool)
            candidate = AsyncExitStack()
            try:
                endpoint = await self._proxy_endpoint(pool[index], candidate)
                if await self._probe_endpoint(endpoint):
                    # Keep this exact checked bridge alive through the download.
                    stack.push_async_callback(candidate.aclose)
                    candidate = None
                    self._proxy = endpoint
                    self.translator._proxy_cursor = (index + 1) % len(pool)
                    logger.info("Website proxy preflight succeeded")
                    return
            except ProxyBridgeError:
                pass
            finally:
                if candidate is not None:
                    await candidate.aclose()
            logger.info("Website proxy preflight failed")
        self.translator._proxy_cursor = (start + attempts) % len(pool)
        raise WebsiteError("None of the configured proxies checked could open the subtitle website. Please try again later.")

    async def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError as exc:
                logger.warning("Could not close the subtitle browser (%s)", type(exc).__name__)
            finally:
                self._browser = None

    async def browser(self):
        if self._browser is not None and not self._browser.is_connected():
            await self._close_browser()
        if self._browser is None:
            self._browser = await self.playwright.chromium.launch(
                executable_path=self.translator.chromium_path,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        return self._browser

    async def _translate_with_early_retry(self, source: PreparedSubtitle | FileSubtitle, language_code: str,
                                          on_progress: Callable[[str], None] | None) -> TranslatedSubtitle:
        # A failed browser launch/navigation may be retried before any file
        # is uploaded. Never retry once the single upload has begun.
        for attempt in range(2):
            try:
                return await self.translator._translate_in_session(source, language_code, self, on_progress)
            except _RetryableWebsiteError:
                if attempt:
                    raise WebsiteError(
                        "The subtitle website could not be opened after retrying. "
                        "It may have timed out or be unavailable; please try again later."
                    ) from None
                logger.info("Retrying website opening before upload with a fresh browser")
                await self._close_browser()
                await asyncio.sleep(1)
        raise AssertionError("unreachable")

    async def translate(self, source: PreparedSubtitle | FileSubtitle, language_code: str, *,
                        on_progress: Callable[[str], None] | None = None) -> TranslatedSubtitle:
        # One upload, one Download file. Never reconstruct the SRT from the
        # preview or combine multiple website downloads.
        try:
            async with AsyncExitStack() as stack:
                if self.translator.website_proxies:
                    _stage("checking website proxies", on_progress)
                    await self._select_proxy(stack)
                result = await self._translate_with_early_retry(source, language_code, on_progress)
        finally:
            # The upload and download must finish in the same browser. Dispose
            # of it before the next queued language, even on failure/cancel.
            self._proxy = None
            await self._close_browser()
        if isinstance(source, PreparedSubtitle):
            suspicious = await asyncio.to_thread(obviously_untranslated_run, source, result, language_code)
            if suspicious:
                raise WebsiteError(
                    "The website download appears to contain untranslated subtitles, "
                    "so no incomplete file was sent. Please try again later."
                )
        if result.possibly_incomplete:
            if isinstance(result.content, Path):
                result.content.unlink(missing_ok=True)
            raise WebsiteError(
                "A long section of the website download remained unchanged and may be untranslated, "
                "so no potentially incomplete file was sent. Please try again later."
            )
        return result


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
        settle_ms: int = 30_000,
        settle_timeout_ms: int = 300_000,
        partial_settle_ms: int | None = None,
        website_proxies: tuple[BrowserProxy | VlessProxy, ...] = (),
    ) -> None:
        self.chromium_path = chromium_path
        self.website_proxies = website_proxies
        self._proxy_cursor = 0
        self._browser_lock = asyncio.Lock()
        # site_url is injectable for local integration tests, not user input.
        self.site_url = site_url
        self.stage_timeout_ms = stage_timeout_ms
        self.translation_timeout_ms = translation_timeout_ms
        self.change_wait_ms = change_wait_ms
        self.retry_change_wait_ms = retry_change_wait_ms
        self.settle_ms = settle_ms
        self.settle_timeout_ms = settle_timeout_ms
        # Only use this longer quiet period when the preview looks partial.
        self.partial_settle_ms = max(settle_ms, partial_settle_ms if partial_settle_ms is not None else settle_ms * 4)

    @asynccontextmanager
    async def session(self):
        """Open one Playwright session for one or several sequential languages."""
        async with self._browser_lock:
            try:
                async with async_playwright() as playwright:
                    session = WebsiteSession(self, playwright)
                    try:
                        yield session
                    finally:
                        await session._close_browser()
            except (PlaywrightError, OSError) as exc:
                logger.warning("Could not open or close the subtitle browser (%s)", type(exc).__name__)
                if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise WebsiteError("The deployment ran out of temporary disk space; no incomplete file was sent.") from None
                raise WebsiteError("The browser could not process the subtitle file. Please try again later.") from None

    async def configure_proxies(self, proxies: tuple[BrowserProxy | VlessProxy, ...], *,
                                on_progress: Callable[[int, int], None] | None = None) -> int:
        """Check every candidate in bounded batches, then atomically replace the pool."""
        if not proxies:
            async with self._browser_lock:
                self.website_proxies = ()
                self._proxy_cursor = 0
            return 0
        working = []
        # Release the browser lock regularly so queued translation jobs can run.
        # Never create thousands of browsers or check tasks in parallel.
        for start in range(0, len(proxies), 2):
            async with self.session() as session:
                for index, proxy in enumerate(proxies[start:start + 2], start=start + 1):
                    if await session._probe_proxy(proxy):
                        working.append(proxy)
                    if on_progress:
                        on_progress(index, len(working))
            await asyncio.sleep(0)
        if working:
            async with self._browser_lock:
                self.website_proxies = tuple(working)
                self._proxy_cursor = 0
        return len(working)

    async def translate(self, source: PreparedSubtitle | FileSubtitle, language_code: str, *,
                        on_progress: Callable[[str], None] | None = None) -> TranslatedSubtitle:
        async with self.session() as session:
            return await session.translate(source, language_code, on_progress=on_progress)

    async def _translate_in_session(self, source: PreparedSubtitle | FileSubtitle, language_code: str,
                                    session: WebsiteSession,
                                    on_progress: Callable[[str], None] | None) -> TranslatedSubtitle:
        stage = _stage("starting the browser", on_progress)
        browser = page = None
        browser_connected = page_closed = None
        output_path = None
        keep_output = False
        try:
            site_source = source if isinstance(source, FileSubtitle) else subtitle_for_website(source)
            expected_cues = site_source.cue_count if isinstance(site_source, FileSubtitle) else len(site_source.cues)
            with tempfile.TemporaryDirectory(prefix="subtranslate-") as temp_dir:
                # Never upload the user's original filename. A symlink avoids
                # copying a potentially huge UTF-8 SRT merely to randomize it.
                input_path = Path(temp_dir) / f"subtitle-{secrets.token_hex(12)}{site_source.extension}"
                if isinstance(site_source, FileSubtitle):
                    output_directory = site_source.work_directory or site_source.path.parent
                    output_path = output_directory / f"download-{secrets.token_hex(12)}.srt"
                    if site_source.encoding == "utf-8-sig":
                        input_path.symlink_to(site_source.path.resolve())
                    else:
                        await run_scan(convert_to_utf8, site_source, input_path)
                else:
                    output_path = Path(temp_dir) / f"download{site_source.extension}"
                    input_path.write_bytes(site_source.utf8)
                browser = await session.browser()
                context_options = {"accept_downloads": True, "locale": "en-US", "service_workers": "block"}
                if session._proxy is not None:
                    context_options["proxy"] = session._proxy.playwright_options()
                context = await browser.new_context(**context_options)
                try:
                    await context.route("**/*", _block_unnecessary_requests)
                    page = await context.new_page()
                    page.set_default_timeout(self.stage_timeout_ms)

                    stage = _stage("opening the subtitle site", on_progress)
                    await page.goto(self.site_url, wait_until="domcontentloaded", timeout=self.stage_timeout_ms)
                    stage = _stage("uploading and parsing the subtitle", on_progress)
                    await page.locator('input[type="file"]').first.set_input_files(str(input_path))
                    # The uploaded file is parsed on the website's next page.
                    # If it stalls, report the problem instead of reuploading.
                    await _wait_for_uploaded_cues(page, expected_cues, self.stage_timeout_ms)
                    selector = page.locator("select.goog-te-combo").first
                    await selector.locator("option").nth(1).wait_for(state="attached", timeout=self.stage_timeout_ms)
                    await _close_vip_popup(page)
                    # Track changes inside the page, not by copying its preview
                    # into Python. Names or numbers can remain unchanged, so
                    # compare every cue when deciding whether to retry.
                    initial_count = await page.evaluate(CACHE_INITIAL_CUES)
                    if initial_count != expected_cues:
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
                        await _wait_for_site_progress(page, PROGRESS_FINISHED, self.translation_timeout_ms)
                    except PlaywrightTimeout:
                        try:
                            # Only booleans about the UI, never subtitle content.
                            state = await page.evaluate(PROGRESS_DIAGNOSTICS)
                            logger.warning("Website completion state: %s", state)
                        except PlaywrightError:
                            logger.info("Website completion state could not be inspected")
                        raise
                    try:
                        # Only indicator booleans, never subtitle content.
                        async with asyncio.timeout(3):
                            state = await page.evaluate(PROGRESS_DIAGNOSTICS)
                        logger.info("Website completion state after animation: %s", state)
                    except (PlaywrightError, TimeoutError):
                        logger.info("Website completion state could not be inspected")
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
                        await _wait_for_site_progress(
                            page, TEXT_STABLE, self.settle_timeout_ms, arg=self.settle_ms, interval=0.5
                        )
                    except PlaywrightTimeout:
                        raise WebsiteError("The website's translation did not finish settling. Please try again.")

                    # Counts only, not subtitle text. These help distinguish
                    # an early-looking site completion from a changed output.
                    counts = await page.evaluate(TEXT_CHANGE_COUNTS)
                    logger.info("Website text changed in %d of %d displayed cues before download",
                                counts["changed"], counts["count"])
                    if (counts["longestUnchanged"] >= 20
                            and self.partial_settle_ms > self.settle_ms):
                        # Even zero changed cues can mean Google's translation
                        # is still pending after the site's success animation.
                        # It can also be a legitimate same-language result.
                        # Give both cases more time before the single download.
                        logger.info("A long unchanged preview section remains; waiting longer before download")
                        try:
                            await _wait_for_site_progress(
                                page, TEXT_STABLE, max(self.settle_timeout_ms, self.partial_settle_ms + 1000),
                                arg=self.partial_settle_ms, interval=0.5,
                            )
                        except PlaywrightTimeout:
                            raise WebsiteError("The website's translation did not finish settling. Please try again.")
                        counts = await page.evaluate(TEXT_CHANGE_COUNTS)
                        logger.info("Website text changed in %d of %d displayed cues after the extra wait",
                                    counts["changed"], counts["count"])
                    stage = _stage("downloading", on_progress)
                    await _close_vip_popup(page)
                    download_button = await _button(page, "Download", 2)
                    async with page.expect_download(timeout=self.stage_timeout_ms) as download_info:
                        await download_button.click()
                    download = await download_info.value
                    if isinstance(site_source, FileSubtitle):
                        # Playwright has already completed this file. Keep its
                        # actual bytes on disk without loading or recreating it.
                        browser_path = await download.path()
                        await run_scan(_retain_download, browser_path, output_path)
                    else:
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
                if isinstance(site_source, FileSubtitle):
                    verified = await run_scan(verify_file_srt, site_source, output_path, language_code)
                    keep_output = True
                    return verified
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
            if stage in {"starting the browser", "opening the subtitle site"}:
                raise _RetryableWebsiteError("The early website flow failed.") from None
            if isinstance(exc, PlaywrightTimeout):
                raise WebsiteError("The subtitle website timed out. Please try again later or use a smaller file.") from None
            raise WebsiteError("Could not complete the subtitle website flow. It may be unavailable or have changed.") from None
        except OSError as exc:
            logger.warning("A local file or browser error occurred while %s (error=%s)", stage, type(exc).__name__)
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise WebsiteError("The deployment ran out of temporary disk space; no incomplete file was sent.") from None
            raise WebsiteError("The browser could not process the subtitle file. Please try again later.") from None
        finally:
            if isinstance(source, FileSubtitle) and output_path is not None and not keep_output:
                output_path.unlink(missing_ok=True)
