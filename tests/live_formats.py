"""Opt-in live checks of the bot's complete format handling with public samples."""

import asyncio
from pathlib import Path

from subtranslate.subtitles import prepare_subtitle, verify_and_merge
from subtranslate.website import WebsiteTranslator

FIXTURES = Path("tests/fixtures")
SAMPLES = ("sample.vtt", "sample.sbv", "sample.ass", "sample.sub", "sample-microfps.sub",
           "sample-subviewer.sub", "sample.stl")


async def main():
    translator = WebsiteTranslator(stage_timeout_ms=45_000, translation_timeout_ms=60_000)
    results = {}
    for filename in SAMPLES:
        try:
            source = prepare_subtitle((FIXTURES / filename).read_bytes(), filename)
            async with asyncio.timeout(150):
                result = await translator.translate(source, "hi")
            checked = verify_and_merge(source, result.content)
            output = prepare_subtitle(result.content, filename)
            if checked.cue_count != 2 or any(
                before.content.strip() == after.content.strip() for before, after in zip(source.cues, output.cues)
            ):
                raise AssertionError("The sample did not produce two changed, valid cues")
            results[filename] = "passed"
            print(f"{filename}: translated {checked.cue_count} cues with matching times", flush=True)
        except Exception as exc:  # noqa: BLE001 - run all independent public samples before reporting
            results[filename] = "failed"
            print(f"{filename}: failed ({type(exc).__name__}): {str(exc)[:300]}", flush=True)
    try:
        # Exercise the optimized batch path against the real site as well:
        # distinct languages must get fresh contexts but share the browser.
        source = prepare_subtitle((FIXTURES / "sample.srt").read_bytes(), "sample.srt")
        async with translator.session() as session:
            reused_browser = None
            for code in ("hi", "es"):
                async with asyncio.timeout(150):
                    result = await session.translate(source, code)
                if reused_browser is None:
                    reused_browser = session._browser
                elif session._browser is not reused_browser:
                    raise AssertionError("The browser was not reused between batch languages")
                checked = verify_and_merge(source, result.content)
                output = prepare_subtitle(result.content)
                if checked.cue_count != 2 or any(
                    before.content.strip() == after.content.strip() for before, after in zip(source.cues, output.cues)
                ):
                    raise AssertionError(f"The {code} batch sample did not produce two translated cues")
        results["batch_reuse"] = "passed"
        print("Batch reuse: Hindi and Spanish translated sequentially in one browser", flush=True)
    except Exception as exc:  # noqa: BLE001 - include an independent live-batch result
        results["batch_reuse"] = "failed"
        print(f"Batch reuse: failed ({type(exc).__name__}): {str(exc)[:300]}", flush=True)
    print("Public live format results:", results, flush=True)
    if "failed" in results.values():
        raise SystemExit("At least one public format sample failed against the live site.")


if __name__ == "__main__":
    asyncio.run(main())
