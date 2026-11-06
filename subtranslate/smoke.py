"""Optional manual test of the actual website without a Telegram bot token."""

import argparse
import asyncio
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
    source = prepare_subtitle(path.read_bytes())
    async with asyncio.timeout(900):
        result = await WebsiteTranslator(os.getenv("CHROMIUM_PATH") or None).translate(source, args.language)
    output = Path(args.output_dir) / translated_filename(path.name, name)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    output.write_bytes(result.content)
    print(f"Saved {result.cue_count} cues to {output}")
    if result.unchanged:
        print("Warning: the site returned unchanged text; review the result.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the actual subtitle website without Telegram")
    parser.add_argument("file", help="Input .srt file")
    parser.add_argument("language", help="Target widget language code, e.g. hi or zh-CN")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
