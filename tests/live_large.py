"""Opt-in live-site test with synthetic Chinese SRTs and separate jobs.

This never uses a user's files, Telegram account, or deployment. Run under
Koyeb-like limits in the opt-in GitHub workflow. The unchunked baseline is
informational only: the site's behavior may change from one run to another.
"""

import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path

import srt

from subtranslate.subtitles import prepare_subtitle
from subtranslate.website import WebsiteTranslator


def synthetic_source(count: int = 405):
    cues = []
    for index in range(1, count + 1):
        start = timedelta(seconds=(index - 1) * 3)
        cues.append(srt.Subtitle(index=index, start=start, end=start + timedelta(seconds=2),
                                 content=f"你好，这是一个简单的测试字幕，编号 {index}。"))
    return prepare_subtitle(srt.compose(cues, reindex=False).encode("utf-8"))


def summary(source, result):
    output = prepare_subtitle(result.content)
    if len(output.cues) != len(source.cues):
        raise AssertionError("The site changed the cue count")
    if any((a.start, a.end) != (b.start, b.end) for a, b in zip(source.cues, output.cues)):
        raise AssertionError("The site changed cue timings")
    unchanged = sum(a.content.strip() == b.content.strip() for a, b in zip(source.cues, output.cues))
    cjk = sum(any(0x3400 <= ord(ch) <= 0x9FFF for ch in cue.content) for cue in output.cues)
    return unchanged, cjk


def print_resource_counts():
    for name in ("memory.peak", "memory.events"):
        try:
            value = (Path("/sys/fs/cgroup") / name).read_text().strip().replace("\n", ", ")
        except OSError:
            continue
        print(f"Resource {name}: {value}", flush=True)


async def main():
    source = synthetic_source()
    translator = WebsiteTranslator(os.environ.get("CHROMIUM_PATH"), stage_timeout_ms=90_000,
                                   translation_timeout_ms=120_000)
    # Compare the old, single-upload behavior without ever sending it to a
    # user. This is a diagnostic, not an assertion about the site's behavior.
    try:
        async with asyncio.timeout(240):
            async with translator.session() as session:
                raw = await translator._translate_in_session(source, "en", session, None)
        unchanged, cjk = summary(source, raw)
        print(f"Single-upload baseline: {len(source.cues)} cues; {unchanged} unchanged, "
              f"{cjk} contain Chinese characters", flush=True)
    except Exception as exc:  # noqa: BLE001 - diagnostic must not prevent the actual recovery test
        print(f"Single-upload baseline could not complete ({type(exc).__name__})", flush=True)

    # Independent translate() calls create and close distinct Playwright
    # sessions, modeling two separate jobs rather than two batch languages.
    for number in (1, 2):
        stages = []
        start = time.monotonic()
        async with asyncio.timeout(600):
            result = await translator.translate(source, "en", on_progress=stages.append)
        unchanged, cjk = summary(source, result)
        part_events = [stage for stage in stages if stage.startswith("part:")]
        if unchanged or cjk or len(part_events) < 3:
            raise AssertionError(
                f"Job {number}: {unchanged} unchanged, {cjk} Chinese-containing cues, "
                f"{len(part_events)} part events"
            )
        print(f"Separate job {number}: {result.cue_count} cues, all changed, "
              f"no Chinese characters, {len(part_events)} parts in {time.monotonic() - start:.1f}s", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        print_resource_counts()
