# SubTranslate Telegram bot

Send an SRT file to the bot, choose a target language, and get an SRT back as, for example, `movie.Hindi.srt`. The bot uploads the file to [translatesubtitles.co](https://translatesubtitles.co/), selects the language on the website, waits for translation, and downloads the result. The website uses Google Translate; the bot does not require a paid translation API key.

## What it does

- Works in a **private Telegram chat**. Send the SRT as a **document**, then pick a popular language or browse **All languages** by letter. Send another file or tap a different language for another translation.
- Supports the languages offered by the site's Google Translate widget. A bundled catalog contains 249 languages; the bot also tries to refresh the list from Google. The actual website selection is checked before each translation. The website handles the source language; it may not translate every language or file perfectly.
- Accepts files up to **1,000,000 bytes** and **3,000 cues**. These are this bot's safety limits for the free instance, not a guarantee about the site's limits. UTF-8 and BOM-marked UTF-16/UTF-32 are supported; other text encodings are detected on a best-effort basis. Save as UTF-8 if a file is rejected.
- Checks that the downloaded file has the same cue count and timestamps. It puts the translated text back into the original cue order with the original indices and timing; the output is UTF-8. If no cue changes after the site signals success, it retries the translate button once. If the result is still unchanged, the file is sent with a warning—it could already be in the target language, or translation could have failed. Always review machine translations.
- Runs one browser at a time, with at most two jobs waiting. If the queue is full, try the button again later. Temporary files are removed after a job. No database or paid translation service is needed.

**Privacy:** Subtitle text is sent to translatesubtitles.co and Google's widget. Do not send confidential content, and only translate files you have permission to use. See the site's [privacy policy](https://translatesubtitles.co/privacypolicy.php). The bot does not intentionally log subtitle text or tokens.

## Deploy on Koyeb's free instance

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token private. Do not paste it into a public repository or chat.
2. Put this repository and its changes on GitHub, then in Koyeb choose **Create App → GitHub**, select the repository and the branch containing this code, and choose the **Dockerfile** builder. The Dockerfile installs Chromium and its runtime libraries.
3. Create a **Web Service**, select the **free** instance in **Frankfurt** or **Washington, D.C.**, expose **HTTP port 8080**, and route `/` to that port. Do not add a worker, volume, database, or paid instance.
4. Add these environment variables in Koyeb (use Secrets for the token and secret):

   | Variable | Value |
   | --- | --- |
   | `BOT_TOKEN` | Your BotFather token |
   | `TELEGRAM_WEBHOOK_SECRET` | A separate, random 16–256 character secret using only letters, numbers, `_` or `-` |
   | `PORT` | `8080` (or match the port configured for your service) |

   Generate a secret locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Do not use the bot token as the webhook secret.
5. Deploy. Koyeb provides `KOYEB_PUBLIC_DOMAIN`; the bot uses it to register `https://<your-domain>/telegram/webhook` with Telegram automatically. Check Koyeb logs for **Telegram webhook registered**. `GET /healthz` is a public health check and does not reveal secrets.
6. Open your bot in Telegram, send `/start`, upload a small `.srt` (for example, `tests/fixtures/sample.srt`), and choose a language. The first request after sleep can take longer. For a private bot, send `/id`, then optionally set `ALLOWED_USER_IDS` to your numeric ID (or a comma-separated list) and redeploy.

**Check billing before deploying.** Koyeb's [instance documentation](https://www.koyeb.com/docs/reference/instances#free-instances) describes one free 512 MB / 0.1 vCPU web instance per organization, with no worker, persistent volume, or custom scaling. It [sleeps after one hour without inbound traffic](https://www.koyeb.com/docs/run-and-scale/scale-to-zero); a Telegram webhook can wake it. Koyeb's [pricing FAQ](https://www.koyeb.com/docs/faqs/pricing) says a credit card is required and the selected plan can be charged on signup. Confirm that your account is on the Starter plan, select the **free** instance, and review current pricing and billing yourself; other plans or resources may incur charges.

### Optional settings

| Variable | Default | Description |
| --- | --- | --- |
| `WEBHOOK_BASE_URL` | Derived from `KOYEB_PUBLIC_DOMAIN` | Public HTTPS origin without a path, e.g. `https://bot.example.com`. Set this when not using Koyeb's default domain. Telegram supports webhook ports 443, 80, 88, and 8443. |
| `ALLOWED_USER_IDS` | Unset | Comma-separated Telegram user IDs. Unset allows any user in private chat. |
| `TRANSLATION_TIMEOUT_SECONDS` | `900` | Total deadline per job, from 60 to 1,000 seconds. |
| `CHROMIUM_PATH` | Docker: `/usr/bin/chromium` | Local system Chromium path; if unset, Playwright uses its installed browser. |

The bot uses a webhook rather than polling, so Telegram requests can wake the service. The language buttons refer back to the original Telegram document instead of relying on an in-memory conversation. However, the **job queue is in memory**: a restart, deployment, crash, or scale-down can interrupt a queued/running job, and an acknowledged webhook update is not durably stored. On a graceful shutdown the bot attempts to notify affected users, but a crash, forced stop or network outage can prevent that notice. If a result does not arrive, tap the language button again or resend the file. This is a small hobby service, not a durable production queue. Large subtitles may be slow or exhaust the 512 MB instance; try splitting them if needed.

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

The included CI workflow is configured to build the deployment image and run a complete **mocked** Telegram → browser → Telegram flow with a 3,000-cue, 999,786-byte file under a 512,000,000-byte memory limit, no swap, and 0.1 CPU. The Docker build has not been run in the development sandbox. You can reproduce the container check locally with Docker:

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

Use `CHROMIUM_PATH=/usr/bin/chromium` if using system Chromium. The command refuses to overwrite an existing output file.

There is also an opt-in **Live website smoke test** in GitHub Actions. To run it from an unmerged branch, include `[live-site-test]` in the pushed commit's message. Once the workflow file exists on the default branch, you can instead open **Actions → Live website smoke test (manual) → Run workflow**. It uploads a tiny, non-private English sample to the real site and checks that the Hindi output changed while preserving timestamps; it does not need a bot token or test Telegram/Koyeb. The site may block GitHub's runner or be unavailable, in which case the check will fail.

The live site and Google widget have no documented API for this workflow and can change, rate-limit, block automated browsers, or be temporarily unavailable; the bot reports failures rather than pretending a translation succeeded. The live Telegram → website flow still needs to be verified with your own bot and deployed service.

### Troubleshooting

- **No response:** check the Koyeb service logs, its public HTTP route and `BOT_TOKEN`. A cold start takes time. Confirm the webhook registered and that no other deployment is changing the webhook for the same bot.
- **Website timed out / could not complete the flow:** the website, Google widget or its selectors may have changed, or may block automation. Try a smaller file and run the manual smoke test from an unrestricted network.
- **File rejected:** send an `.srt` document smaller than 1 MB, with no more than 3,000 valid cues, and save it as UTF-8. A translated file with missing cues or modified timestamps is intentionally not delivered.
- **Interrupted job:** free-instance storage and the queue are temporary. Tap the language button again; if Telegram no longer provides the original message, resend the file.

The selector flow was informed by [MutennRoshii/subtitle-translator](https://github.com/MutennRoshii/subtitle-translator); see `THIRD_PARTY_NOTICES.md`.
