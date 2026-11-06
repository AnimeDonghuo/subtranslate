# SubTranslate Telegram bot

Send a subtitle file to the bot, choose a target language, and get a translated file back, for example `movie.Hindi.srt`. The bot uses [translatesubtitles.co](https://translatesubtitles.co/) to select the language, translate, and download the result. The website uses Google Translate; the bot does not require a paid translation API key.

## What it does

- Works in a **private Telegram chat**. Send the file as a **document**. The picker shows **five language buttons per page**, with Previous/Next navigation. Tap a language for a single translation, or **Select all languages** and confirm to receive one file at a time, in order. Use `/cancel` to remove queued batches and stop an active batch after its current language.
- The bundled catalog currently contains 249 Google widget languages and the bot attempts to refresh it on startup. Every selection is checked against the site's actual dropdown; stale entries are skipped in a batch. The website detects the source language and translation quality can vary.
- Accepts text **SRT, ASS, SBV, VTT, MicroDVD or SubViewer SUB, and Spruce STL**. Binary VobSub (`.sub`/`.idx`) and binary EBU STL are not supported. The site misparsed the tested SUB variants, so SUB cue text is temporarily converted to SRT for the website and restored to the original SUB format with its original frame numbers or timestamps. Other formats are uploaded in their original format.
- Input is limited to **1,000,000 bytes** (also after conversion to UTF-8 or the SUB bridge) and **3,000 cues**. These are this bot's safety limits, not a guarantee about the site's limits. UTF-8 and BOM-marked UTF-16/UTF-32 are supported; other text encodings are detected on a best-effort basis. Save as UTF-8 if a file is rejected.
- Clicks the website's **Download** button and checks the downloaded file's cue count and timestamps; a mismatch is rejected. For SRT, VTT, SBV and text STL, the website's downloaded bytes are sent unchanged if already UTF-8, otherwise they are converted to UTF-8. ASS restores source styles and dialogue metadata; SUB uses the SRT bridge described above, so those two formats are reconstructed from the downloaded translation. The site may change cue numbers, inline formatting, comments, VTT settings or other metadata, so review the file. Matching cues and timings do not guarantee translation quality or that every cue changed. The file caption reports how many cue texts differ from the original, not how many are correct; names and shared words may legitimately remain unchanged. If all text is unchanged after success, the bot retries once and then warns.
- A single Telegram status message shows the current processing stage and a stage bar; it does **not** claim an exact translation percentage. For Select all, it also shows how many languages have been processed and the current language. The site does not expose a reliable translation percentage or ETA.
- Runs **one browser at a time**, with at most two jobs waiting. A Select all batch reuses the browser for up to ten languages, with a fresh isolated context for each, then recycles it to limit memory growth. It still translates and sends files one at a time. A batch occupies the worker until it finishes, is canceled, or encounters repeated failures, so other jobs may wait a long time. It can take hours and generate hundreds of messages. Temporary files are removed after each language; no database or paid translation service is needed.

**Privacy:** Subtitle text is sent to translatesubtitles.co and Google's widget. Do not send confidential content, and only translate files you have permission to use. See the site's [privacy policy](https://translatesubtitles.co/privacypolicy.php). The bot does not intentionally log subtitle text or tokens.

## Deploy on Koyeb's free instance

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token private. Do not paste it into a public repository or chat.
2. Put this repository and its changes on GitHub, then in Koyeb choose **Create App → GitHub**, select the repository and the branch containing this code, and choose the **Dockerfile** builder. The Dockerfile installs Chromium and its runtime libraries.
3. Create a **Web Service**, select the **free** instance in **Frankfurt** or **Washington, D.C.**, expose **HTTP port 8080**, and route `/` to that port. Do not add a worker, volume, database, or paid instance.
4. Add these environment variables in Koyeb (use a Secret for the bot token):

   | Variable | Value |
   | --- | --- |
   | `BOT_TOKEN` | Your BotFather token |
   | `PORT` | `8080` (or match the port configured for your service) |

   No Telegram API ID, API hash, or separate webhook secret is required. Webhook authentication is derived automatically from the bot token and remains stable across restarts. If upgrading an older version, you can remove `TELEGRAM_WEBHOOK_SECRET`; the bot registers the updated webhook on startup.
5. Deploy. Koyeb provides `KOYEB_PUBLIC_DOMAIN`; the bot uses it to register `https://<your-domain>/telegram/webhook` with Telegram automatically. Check Koyeb logs for **Telegram webhook registered**. The public `GET /healthz` endpoint is a liveness check: it returns HTTP 200 even while the bot is connecting, with a JSON status showing the connection phase. `GET /readyz` returns HTTP 200 only after the webhook is registered, or 503 while connecting or after an error. Neither endpoint reveals secrets. Use `/healthz` (or `/`) for Koyeb's health check, not `/readyz`, so a slow Telegram connection does not cause a restart loop.
6. Open your bot in Telegram, send `/start`, upload a small `.srt` (for example, `tests/fixtures/sample.srt`), and choose a language. The first request after sleep can take longer. For a private bot, send `/id`, then optionally set `ALLOWED_USER_IDS` to your numeric ID (or a comma-separated list) and redeploy.

**Check billing before deploying.** Koyeb's [instance documentation](https://www.koyeb.com/docs/reference/instances#free-instances) describes one free 512 MB / 0.1 vCPU web instance per organization, with no worker, persistent volume, or custom scaling. It [sleeps after one hour without inbound traffic](https://www.koyeb.com/docs/run-and-scale/scale-to-zero); a Telegram webhook can wake it. Koyeb's [pricing FAQ](https://www.koyeb.com/docs/faqs/pricing) says a credit card is required and the selected plan can be charged on signup. Confirm that your account is on the Starter plan, select the **free** instance, and review current pricing and billing yourself; other plans or resources may incur charges.

### Optional settings

| Variable | Default | Description |
| --- | --- | --- |
| `WEBHOOK_BASE_URL` | Derived from `KOYEB_PUBLIC_DOMAIN` | Public HTTPS origin without a path, e.g. `https://bot.example.com`. Set this when not using Koyeb's default domain. Telegram supports webhook ports 443, 80, 88, and 8443. |
| `ALLOWED_USER_IDS` | Unset | Comma-separated Telegram user IDs. Unset allows any user in private chat. |
| `TRANSLATION_TIMEOUT_SECONDS` | `900` | Deadline per single language, from 60 to 1,000 seconds; a Select all batch has no fixed total deadline. |
| `CHROMIUM_PATH` | Docker: `/usr/bin/chromium` | Local system Chromium path; if unset, Playwright uses its installed browser. |

The bot uses a webhook rather than polling, so Telegram requests can wake the service. The language buttons refer back to the original Telegram document instead of relying on an in-memory conversation. During an active batch, the bot makes a best-effort health request to its public URL every 20 minutes to reduce idle scale-down; this is not a guarantee. The **job queue and batch progress are in memory**: a restart, deployment, crash, or scale-down can interrupt work, and an acknowledged webhook update is not durably stored. On graceful shutdown the bot attempts to notify affected users, but a crash, forced stop or network outage can prevent that notice. If a result does not arrive, tap a language button again or resend the file. Restarting a batch begins from the first language, not where it stopped. This is a small hobby service, not a durable production queue. Large subtitles may be slow or exhaust the 512 MB instance; try splitting them if needed.

## Local development and testing

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

The browser integration tests use a **local mock website**, not the real translation service. To run those tests too, install Chromium and set `TEST_CHROMIUM_PATH`, for example:

```sh
TEST_CHROMIUM_PATH=/usr/bin/chromium python -m pytest -q
```

The included CI workflow builds the deployment image and runs a complete **mocked** Telegram → browser → Telegram flow with a 3,000-cue, 999,786-byte file under a 512,000,000-byte memory limit, no swap, and 0.1 CPU. That container check has passed on GitHub Actions. You can reproduce it locally with Docker:

```sh
docker build -t subtranslate-local .
docker run --rm --network none --memory 512000000 --memory-swap 512000000 --cpus 0.1 \
  --mount type=bind,source="$PWD/tests",target=/smoke,readonly \
  --env PYTHONPATH=/app subtranslate-local python /smoke/resource_smoke.py
```

A similar Linux cgroup check passed locally with a test Chromium binary; it is not a guarantee that the live website or every Chromium/Docker version fits. CI runs on pushes and pull requests; GitHub Actions usage is subject to your repository's quotas and billing settings.

For a manual test of the **actual** website from a machine with Internet access, install Chromium or run `python -m playwright install chromium`, then:

```sh
# This bundled sample contains no private information.
python -m subtranslate.smoke tests/fixtures/sample.srt hi --output-dir .
```

Use `CHROMIUM_PATH=/usr/bin/chromium` if using system Chromium. The command logs the current website stage and refuses to overwrite an existing output file. Use `--timeout-seconds` and `--translation-timeout-seconds` to shorten its deadlines when diagnosing a stalled site.

There is also an opt-in **Live website smoke test** in GitHub Actions. To run it from an unmerged branch, include `[live-site-test]` in the pushed commit's message. Once the workflow file exists on the default branch, you can instead open **Actions → Live website smoke test (manual) → Run workflow**. It tests tiny, non-private English samples in each supported format, including SUB via the SRT bridge, and checks that the Hindi output changed while preserving cue timings. It does not need a bot token or test Telegram/Koyeb. The site may block GitHub's runner or be unavailable, in which case the check will fail.

The live site and Google widget have no documented API for this workflow and can change, rate-limit, block automated browsers, or be temporarily unavailable; the bot reports failures rather than pretending a translation succeeded. The automated tests do not connect to your Telegram bot or deployed Koyeb service.

### Troubleshooting

- **No response:** check the Koyeb service logs and its public HTTP route. A cold start takes time. Confirm **Telegram webhook registered**, check `/readyz`, and ensure no other deployment is changing the webhook for the same bot.
- **Telegram initialization failed (`TimedOut`):** this indicates a network timeout, not proof of a bad token. The HTTP service starts anyway and retries Telegram with increasing delays. Wait for **Telegram webhook registered**; if it keeps timing out for several minutes, investigate the Koyeb instance's DNS and outbound HTTPS access to `api.telegram.org` (port 443) or contact Koyeb support. An HTTP 200 from `/healthz` alone does not mean the bot is ready; `/readyz` will remain 503 until registration succeeds. Do not post your bot token in logs or chat.
- **Webhook registration keeps retrying:** check the error type in the logs and confirm the public HTTPS route and `WEBHOOK_BASE_URL` (if set). Network timeouts can also require the connectivity checks above.
- **Telegram rejected `BOT_TOKEN` / `/readyz` reports `invalid_token`:** Telegram explicitly rejected the token. Check the BotFather token in Koyeb's secret configuration and redeploy; do not share it in chat.
- **Website timed out / could not complete the flow:** the website, Google widget or its selectors may have changed, or may block automation. Try a smaller file and run the manual smoke test from an unrestricted network. The stage bar is not a translation percentage; the website's translation stage may take much longer than other steps.
- **The translated file has a different size:** translated text may use different numbers of characters and UTF-8 bytes, and the website may change line endings or formatting. Size alone does not show whether cues are missing. The bot checks cue count and timestamps but cannot guarantee that every line was translated correctly. Separate website translations can also differ; comparing the files is the only way to determine the specific cause. Older bot versions rebuilt SRT files after downloading them; current versions send the site's UTF-8 SRT download directly after validation.
- **File rejected:** send a supported text subtitle document under 1 MB, with no more than 3,000 valid cues, and save it as UTF-8. Binary VobSub/EBU STL are unsupported. A translated file with missing cues or modified timestamps is intentionally not delivered.
- **Interrupted job:** free-instance storage and the queue are temporary. Tap the language button again; if Telegram no longer provides the original message, resend the file. A restarted Select all batch begins again from the first language.
- **Long batch:** `/cancel` removes your queued all-language jobs and requests the active one to stop after its current language. It does not cancel individual translations.

The selector flow was informed by [MutennRoshii/subtitle-translator](https://github.com/MutennRoshii/subtitle-translator); see `THIRD_PARTY_NOTICES.md`.
