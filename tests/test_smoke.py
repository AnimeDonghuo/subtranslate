import argparse
import subprocess
import sys
from unittest.mock import AsyncMock

from subtranslate import smoke
from subtranslate.subtitles import TranslatedSubtitle


async def test_smoke_uses_custom_deadlines_without_touching_network(tmp_path, monkeypatch):
    source = tmp_path / "sample.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
    seen = {}

    class FakeTranslator:
        def __init__(self, chromium_path, *, translation_timeout_ms):
            seen["timeout_ms"] = translation_timeout_ms

        async def translate(self, prepared, language):
            seen["language"] = language
            return TranslatedSubtitle(prepared.utf8, len(prepared.cues), False)

    monkeypatch.setattr(smoke, "WebsiteTranslator", FakeTranslator)
    monkeypatch.setattr(smoke.LanguageCatalog, "refresh", AsyncMock())
    args = argparse.Namespace(file=str(source), language="hi", output_dir=str(tmp_path),
                              timeout_seconds=60, translation_timeout_seconds=30)
    await smoke._run(args)
    assert seen == {"timeout_ms": 30_000, "language": "hi"}
    assert (tmp_path / "sample.Hindi.srt").exists()


def test_smoke_rejects_invalid_deadlines_before_network_access():
    result = subprocess.run(
        [sys.executable, "-m", "subtranslate.smoke", "unused.srt", "hi", "--timeout-seconds", "59"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "--timeout-seconds must be between" in result.stderr
