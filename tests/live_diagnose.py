"""Inspect the real website with the public synthetic sample, without Telegram."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from subtranslate.website import (
    _block_unnecessary_requests,
    _button,
    _close_vip_popup,
    _wait_for_uploaded_cues,
)


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(accept_downloads=True, locale="en-US", service_workers="block")
            await context.route("**/*", _block_unnecessary_requests)
            page = await context.new_page()
            page.set_default_timeout(60_000)
            page.on("pageerror", lambda error: print("PAGE ERROR:", str(error)[:500], flush=True))
            page.on("console", lambda msg: print("CONSOLE ERROR:", msg.text[:500], flush=True)
                    if msg.type == "error" else None)
            page.on("requestfailed", lambda req: print("REQUEST FAILED:", urlsplit(req.url).hostname,
                                                        req.failure, flush=True)
                    if "google" in (urlsplit(req.url).hostname or "") else None)
            await page.goto("https://translatesubtitles.co/", wait_until="domcontentloaded")
            await page.locator('input[type="file"]').first.set_input_files(str(Path("tests/fixtures/sample.srt")))
            await _wait_for_uploaded_cues(page, 2, 60_000)
            combo = page.locator("select.goog-te-combo").first
            await combo.locator("option").nth(1).wait_for(state="attached")
            await _close_vip_popup(page)
            before = await page.locator("td.notranslate.target-sub").all_text_contents()
            await combo.select_option("hi")
            print("Selected Hindi; waiting 8 seconds for the Google widget", flush=True)
            await page.wait_for_timeout(8000)
            button = await _button(page, "Translate", 1)
            print("TRANSLATE BUTTON:", await button.evaluate("el => el.outerHTML"), flush=True)
            print("PAGE SCRIPTS:", await page.evaluate("() => [...document.scripts].map(s => s.src).filter(Boolean)"), flush=True)
            await button.click()
            for tick in range(10):
                await page.wait_for_timeout(2000)
                state = await page.evaluate("""(before) => {
                    const loader = document.querySelector('#loader');
                    const success = document.querySelector('h4.skiptranslate.success-msg');
                    const visible = el => !!(el && el.getClientRects().length &&
                        getComputedStyle(el).visibility !== 'hidden');
                    const cells = [...document.querySelectorAll('td.notranslate.target-sub')];
                    return {loaderVisible: visible(loader), loaderStyle: loader?.getAttribute('style'),
                            successVisible: visible(success), successText: success?.textContent?.trim(),
                            targetChanged: cells.some((el, i) => el.textContent.trim() !== before[i].trim()),
                            targets: cells.map(el => el.textContent.trim()),
                            language: document.querySelector('select.goog-te-combo')?.value};
                }""", before)
                print(f"STATE {2 * (tick + 1)}s:", state, flush=True)
                if state["successVisible"]:
                    break
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
