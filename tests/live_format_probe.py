"""Probe public, synthetic samples against the real third-party site.

This is opt-in, not a unit test. The output includes sample text and must never
be used with a private file. It probes each advertised extension separately so
one failure doesn't hide the other formats.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from subtranslate.languages import LanguageCatalog
from subtranslate.website import (
    PROGRESS_FINISHED,
    PROGRESS_WATCH,
    TEXT_CHANGED,
    _block_unnecessary_requests,
    _button,
    _close_vip_popup,
    _wait_for_uploaded_cues,
)

FIXTURES = Path("tests/fixtures")


async def probe(browser, filename):
    extension = filename
    context = await browser.new_context(accept_downloads=True, locale="en-US", service_workers="block")
    await context.route("**/*", _block_unnecessary_requests)
    page = await context.new_page()
    page.set_default_timeout(30_000)
    try:
        async with asyncio.timeout(100):
            await page.goto("https://translatesubtitles.co/", wait_until="domcontentloaded")
            await page.locator('input[type="file"]').first.set_input_files(str(FIXTURES / filename))
            await _wait_for_uploaded_cues(page, 2, 25_000)
            cells = page.locator("td.notranslate.target-sub")
            before = await cells.all_text_contents()
            print(f"{extension}: parsed {len(before)} cues: {before!r}", flush=True)
            selector = page.locator("select.goog-te-combo").first
            await selector.locator("option").nth(1).wait_for(state="attached")
            if filename == "sample.vtt":
                site_codes = set(await selector.locator("option").evaluate_all("els => els.map(el => el.value).filter(Boolean)"))
                catalog_codes = {code for code, _ in LanguageCatalog().items()}
                print(f"Language codes: site={len(site_codes)}, catalog={len(catalog_codes)}, "
                      f"site-only={sorted(site_codes - catalog_codes)}, catalog-only={sorted(catalog_codes - site_codes)}", flush=True)
            await selector.select_option("hi")
            await _close_vip_popup(page)
            await page.evaluate(PROGRESS_WATCH)
            await (await _button(page, "Translate", 1)).click()
            await page.wait_for_function(PROGRESS_FINISHED, timeout=30_000, polling=250)
            await page.wait_for_function(TEXT_CHANGED, arg=before, timeout=15_000, polling=500)
            await page.wait_for_timeout(5_000)
            await _close_vip_popup(page)
            async with page.expect_download(timeout=30_000) as download_info:
                await (await _button(page, "Download", 2)).click()
            download = await download_info.value
            data = Path(await download.path()).read_bytes()
            print(f"{extension}: download name={download.suggested_filename!r}, bytes={len(data)}, "
                  f"public sample={data[:1500].decode('utf-8', errors='replace')!r}", flush=True)
            return True
    except Exception as exc:  # noqa: BLE001 - report each independent public sample
        print(f"{extension}: FAILED ({type(exc).__name__}): {str(exc)[:300]}", flush=True)
        try:
            count = await page.locator("td.notranslate.target-sub").count()
            body = (await page.locator("body").inner_text(timeout=2000))[:500]
            print(f"{extension}: page={page.url!r}, cells={count}, public page excerpt={body!r}", flush=True)
        except Exception as diagnostic_error:  # noqa: BLE001
            print(f"{extension}: page diagnostics unavailable ({type(diagnostic_error).__name__})", flush=True)
        return False
    finally:
        await context.close()


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            results = {}
            for filename in ("sample.vtt", "sample.sbv", "sample.ass", "sample.sub", "sample-microfps.sub",
                             "sample-subviewer.sub", "sample.stl"):
                results[filename] = await probe(browser, filename)
        finally:
            await browser.close()
    print("Public live format probe results:", results, flush=True)
    if not all(results.values()):
        raise SystemExit("At least one advertised format could not be translated and downloaded.")


if __name__ == "__main__":
    asyncio.run(main())
