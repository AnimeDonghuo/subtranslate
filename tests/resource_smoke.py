"""Exercise the complete mocked bot flow under an external resource limit.

This uses the runtime dependencies only. Run it inside the Docker image with
network disabled, a 512 MB memory limit, and 0.1 CPU; see README.md. It uses
real Chromium and a local stand-in for the third-party website, not the live
site or Telegram. The fake transport is shared with the normal bot tests.
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import srt
import test_bot_flow as fake_telegram
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import subtranslate.web as bot_web
from subtranslate.config import Settings
from subtranslate.languages import LanguageCatalog
from subtranslate.subtitles import MAX_CUES, MAX_INPUT_BYTES
from subtranslate.website import WebsiteTranslator


async def wait_for_result(stub: fake_telegram.TelegramStub) -> None:
    """Fail promptly if the bot reports an error rather than sending a file."""
    inspected = 0
    while not stub.document_sent.is_set():
        for action, params in stub.calls[inspected:]:
            text = params.get("text", "")
            if action == "sendMessage" and text.startswith((
                "Could not translate:", "Translation timed out.",
                "Telegram could not", "Something went wrong",
            )):
                raise AssertionError(f"The mocked job failed: {text}")
        inspected = len(stub.calls)
        await asyncio.sleep(0.25)


async def main() -> None:
    chromium = os.getenv("TEST_CHROMIUM_PATH") or os.getenv("CHROMIUM_PATH") or shutil.which("chromium")
    if not chromium:
        raise RuntimeError("Set TEST_CHROMIUM_PATH or CHROMIUM_PATH to a Chromium executable")

    # Near the bot's byte and cue ceilings, without storing a large fixture.
    cue_text = "x" * 240
    fake_telegram.SOURCE = "".join(
        f"{index}\n00:00:01,000 --> 00:00:02,000\nOriginal subtitle cue {index}: {cue_text}"
        "\nSecond line for readability.\n\n"
        for index in range(1, MAX_CUES + 1)
    ).encode()
    assert 0.99 * MAX_INPUT_BYTES < len(fake_telegram.SOURCE) <= MAX_INPUT_BYTES
    fake_telegram.DOCUMENT_MESSAGE["document"]["file_size"] = len(fake_telegram.SOURCE)
    print(f"Mocked bot flow: {MAX_CUES:,} cues, {len(fake_telegram.SOURCE):,} input bytes", flush=True)

    html = (Path(__file__).parent / "fixtures" / "mock_website.html").read_text(encoding="utf-8")
    mock = web.Application()

    async def serve(request: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html")

    mock.router.add_get("/", serve)
    async with TestServer(mock) as mock_server:
        stub = fake_telegram.TelegramStub()
        settings = Settings("12345:TESTTOKEN", "https://example.com")
        browser = WebsiteTranslator(chromium, site_url=str(mock_server.make_url("/")))
        with patch.object(bot_web, "WebsiteTranslator", return_value=browser), \
             patch.object(LanguageCatalog, "refresh", new=AsyncMock()):
            async with TestClient(TestServer(bot_web.create_web_app(settings, telegram_request=stub))) as client:
                await asyncio.wait_for(stub.webhook_set.wait(), 60)
                headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
                response = await client.post(
                    "/telegram/webhook", headers=headers,
                    json={"update_id": 20, "message": fake_telegram.DOCUMENT_MESSAGE},
                )
                assert response.status == 200
                await asyncio.wait_for(stub.picker_sent.wait(), 60)
                start = time.monotonic()
                response = await client.post(
                    "/telegram/webhook", headers=headers, json=fake_telegram.callback_update(21),
                )
                assert response.status == 200
                await asyncio.wait_for(wait_for_result(stub), 300)
                upload = next(iter(stub.uploads[0].values()))
                assert upload[0] == "movie.Hindi.srt"
                cues = list(srt.parse(upload[1].decode("utf-8")))
                assert len(cues) == MAX_CUES
                assert cues[0].index == 1 and cues[-1].index == MAX_CUES
                assert all(cue.content == "Hola mundo" for cue in cues)
                print(f"Success: {len(upload[1]):,} output bytes in {time.monotonic() - start:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
