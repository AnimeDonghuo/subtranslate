import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

HTML = (Path(__file__).parent / "fixtures" / "mock_website.html").read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def mock_site():
    app = web.Application()
    async def serve(request):
        return web.Response(text=HTML, content_type="text/html")
    app.router.add_get("/", serve)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/"))
    finally:
        await server.close()


@pytest.fixture
def chromium_path():
    path = os.getenv("TEST_CHROMIUM_PATH") or os.getenv("CHROMIUM_PATH") or shutil.which("chromium")
    if not path:
        pytest.skip("Set TEST_CHROMIUM_PATH to run the real-browser integration tests")
    return path
