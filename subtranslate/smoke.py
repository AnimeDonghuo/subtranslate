"""Optional manual test of the actual website without a Telegram bot token."""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .languages import LanguageCatalog
from .subtitles import prepare_subtitle, translated_filename
from .website import WebsiteTranslator


async def _run(args) -> None:
    catalog = LanguageCatalog()
    await catalog.refresh()
    name = catalog.get(args.language)
    if not name:
        raise ValueError("Unknown target language code; check subtranslate/languages.tsv")
    path = Path(args.file)
    source = prepare_subtitle(path.read_bytes(), path.name)
    async with asyncio.timeout(args.timeout_seconds):
        result = await WebsiteTranslator(
            os.getenv("CHROMIUM_PATH") or None,
            translation_timeout_ms=args.translation_timeout_seconds * 1000,
        ).translate(source, args.language)
    output = Path(args.output_dir) / translated_filename(path.name, name)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    output.write_bytes(result.content)
    print(f"Saved {result.cue_count} cues to {output}")
    if result.unchanged or result.possibly_incomplete:
        print("Warning: a long section of text did not change; the result may be incomplete. Please review it.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the actual subtitle website without Telegram")
    parser.add_argument("file", help="Input text subtitle: .srt, .ass, .sub, .sbv, .vtt or .stl")
    parser.add_argument("language", help="Target widget language code, e.g. hi or zh-CN")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Overall deadline (60–1,000 seconds)")
    parser.add_argument(
        "--translation-timeout-seconds", type=int, default=None,
        help="Deadline for the site's translation step (defaults to the overall timeout)",
    )
    args = parser.parse_args()
    if not 60 <= args.timeout_seconds <= 1000:
        parser.error("--timeout-seconds must be between 60 and 1,000")
    if args.translation_timeout_seconds is None:
        args.translation_timeout_seconds = args.timeout_seconds
    if not 1 <= args.translation_timeout_seconds <= args.timeout_seconds:
        parser.error("--translation-timeout-seconds must be between 1 and the overall timeout")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
