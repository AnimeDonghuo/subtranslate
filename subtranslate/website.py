"""Drive the public translatesubtitles.co website; this is not an API client."""

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .subtitles import (
    MAX_OUTPUT_BYTES,
    PreparedSubtitle,
    TranslatedSubtitle,
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


def _stage(name: str) -> str:
    # Stage names are fixed strings: never log subtitle text or filenames.
    logger.info("Subtitle website: %s", name)
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
            raise PlaywrightTimeout("The website did not display all uploaded subtitle cues")
        try:
            await cells.nth(expected_count - 1).wait_for(
                state="attached", timeout=min(1000, remaining * 1000)
            )
        except PlaywrightTimeout:
            # A VIP interstitial may be what prevents the next page loading.
            continue


async def _button(page, label: str, number: int):
    expression = re.compile(rf"^\s*(?:{number}\.\s*)?{label}\s*$", re.IGNORECASE)
    button = page.get_by_role("button", name=expression).first
    if await button.count():
        return button
    # Some versions of the site use links or spans rather than buttons.
    return page.get_by_text(expression).first


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

TEXT_CHANGED = r"""(before) => {
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

    async def translate(self, source: PreparedSubtitle, language_code: str) -> TranslatedSubtitle:
        stage = _stage("starting the browser")
        try:
            with tempfile.TemporaryDirectory(prefix="subtranslate-") as temp_dir:
                input_path = Path(temp_dir) / "input.srt"
                output_path = Path(temp_dir) / "download.srt"
                input_path.write_bytes(source.utf8)
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=self.chromium_path,
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                    )
                    try:
                        context = await browser.new_context(
                            accept_downloads=True, locale="en-US", service_workers="block"
                        )
                        await context.route("**/*", _block_unnecessary_requests)
                        page = await context.new_page()
                        page.set_default_timeout(self.stage_timeout_ms)

                        stage = _stage("opening the subtitle site")
                        await page.goto(self.site_url, wait_until="domcontentloaded", timeout=self.stage_timeout_ms)
                        stage = _stage("uploading and parsing the subtitle")
                        await page.locator('input[type="file"]').first.set_input_files(str(input_path))
                        # The uploaded file is parsed on the website's next page.
                        target_cells = page.locator("td.notranslate.target-sub")
                        await _wait_for_uploaded_cues(page, len(source.cues), self.stage_timeout_ms)
                        selector = page.locator("select.goog-te-combo").first
                        await selector.locator("option").nth(1).wait_for(state="attached", timeout=self.stage_timeout_ms)
                        await _close_vip_popup(page)
                        # Compare every cue: the first few may be names or numbers
                        # which legitimately stay the same after translation.
                        before = await target_cells.evaluate_all(
                            "els => els.map(el => (el.textContent || '').trim())"
                        )
                        if len(before) != len(source.cues):
                            raise WebsiteError("The website parsed a different number of cues. Please try again.")

                        stage = _stage("selecting the target language")
                        values = await selector.locator("option").evaluate_all("els => els.map(el => el.value)")
                        if language_code not in values:
                            raise LanguageUnavailable(
                                "That language is no longer available on the subtitle site. Please choose another."
                            )
                        await selector.select_option(value=language_code)
                        await _close_vip_popup(page)

                        stage = _stage("translating")
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
                        # Some site versions signal success early or need a second
                        # click for very short files. Retry once only if no cue has
                        # changed; same-language translations may also be unchanged.
                        try:
                            await page.wait_for_function(
                                TEXT_CHANGED, arg=before, timeout=self.change_wait_ms, polling=500
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
                                        TEXT_CHANGED, arg=before, timeout=self.retry_change_wait_ms, polling=500
                                    )
                                except PlaywrightTimeout:
                                    pass
                        try:
                            await page.wait_for_function(
                                TEXT_STABLE, arg=self.settle_ms, timeout=self.settle_timeout_ms, polling=500
                            )
                        except PlaywrightTimeout:
                            raise WebsiteError("The website's translation did not finish settling. Please try again.")

                        stage = _stage("downloading")
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
                        await browser.close()
                # Refuse missing cues or changed times rather than silently
                # sending broken subtitles to the user.
                return verify_and_merge(source, downloaded)
        except LanguageUnavailable:
            raise
        except WebsiteError:
            raise
        except PlaywrightTimeout as exc:
            logger.warning("Subtitle website timed out while %s", stage)
            raise WebsiteError("The subtitle website timed out. Please try again later or use a smaller file.") from exc
        except PlaywrightError as exc:
            logger.warning("Subtitle website interaction failed while %s", stage)
            raise WebsiteError("Could not complete the subtitle website flow. It may be unavailable or have changed.") from exc
        except OSError as exc:
            logger.warning("A local file or browser error occurred while %s", stage)
            raise WebsiteError("The browser could not process the subtitle file. Please try again later.") from exc
