"""Opt-in live-site test of one direct download per synthetic SRT job.

This never uses a user's files, Telegram account, or deployment. It runs under
Koyeb-like limits in GitHub. A failure may reflect an external site outage or
an incomplete site download; it is not proof of the specific user's root cause.
"""

import asyncio
import logging
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import srt

from subtranslate.large_srt import prepare_file_srt
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
    data = result.content.read_bytes() if isinstance(result.content, Path) else result.content
    output = prepare_subtitle(data)
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
    original = synthetic_source()
    failures = 0
    with tempfile.TemporaryDirectory(prefix="subtranslate-live-test-") as directory:
        path = Path(directory) / "synthetic.srt"
        path.write_bytes(original.utf8)
        source = prepare_file_srt(path)
        # Independent public-sample jobs each use one upload and one download.
        # Compare production's adaptive wait with a fixed longer quiet time;
        # this is not evidence about a particular user's earlier file.
        for number, settle_ms in ((1, 30_000), (2, 120_000)):
            stages = []
            start = time.monotonic()
            translator = WebsiteTranslator(os.environ.get("CHROMIUM_PATH"), stage_timeout_ms=90_000,
                                           translation_timeout_ms=300_000, settle_ms=settle_ms,
                                           partial_settle_ms=120_000)
            print(f"Separate job {number}: quiet wait {settle_ms // 1000}s "
                  "(extended to 120s of quiet if the preview looks partial)", flush=True)
            try:
                async with asyncio.timeout(600):
                    result = await translator.translate(source, "en", on_progress=stages.append)
                unchanged, cjk = summary(original, result)
                if (unchanged or cjk or stages.count("uploading and parsing the subtitle") != 1
                        or stages.count("downloading") != 1):
                    raise AssertionError(
                        f"{unchanged} unchanged, {cjk} Chinese-containing cues, "
                        f"{stages.count('uploading and parsing the subtitle')} upload(s), "
                        f"{stages.count('downloading')} download(s)"
                    )
                print(f"Separate job {number}: {result.cue_count} cues, all changed, "
                      f"one website download in {time.monotonic() - start:.1f}s", flush=True)
                result.content.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - finish both independent public experiments
                failures += 1
                print(f"Separate job {number} failed in {time.monotonic() - start:.1f}s "
                      f"({type(exc).__name__}): {str(exc)[:300]}", flush=True)
    if failures:
        raise SystemExit(f"{failures} direct website experiment(s) failed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    finally:
        print_resource_counts()
