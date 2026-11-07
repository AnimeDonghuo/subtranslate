# SubTranslate Telegram bot

Send a subtitle as a Telegram document, choose a language, and receive the website's downloaded file, for example `movie.Hindi.srt`. The bot automates [translatesubtitles.co](https://translatesubtitles.co/); it does not use a different translation service.

## How it works

- Use a **private chat**. The language picker has five buttons per page. Choose one language, or confirm **Select all languages** to translate and send one file at a time, sequentially. `/cancel` removes your queued jobs and stops active work.
- For an SRT, the bot uploads the **whole file once**, selects the language, clicks Translate, waits for the visible website/Google loading animations to finish and for text changes to settle, and clicks **Download**. If a long preview section appears unchanged (including when no cues have changed), it waits longer before that one download. This can help with delayed translations, but cannot fix a failure of the website or Google Translate. It sends the single downloaded SRT **without assembling parts or rewriting its bytes**. The on-page preview is not the delivered file.
- The bot checks cue count and timestamps, and refuses a download with a strong sign of missing translation or a long unchanged section mixed with changed text. An entirely unchanged result is flagged because it might already be in the selected language. These are heuristics: they can reject legitimate unchanged sections, and same-script partial translations or incorrect words can still escape detection. Always review machine translation; completeness and quality cannot be guaranteed.
- SRT files have **no bot-imposed total byte-size or cue-count cap**. Input and downloaded files are validated incrementally on temporary disk; the browser and website still process the original file as a single upload. A single cue must fit available memory. There is no guarantee that the site, browser, Telegram, or a particular host can process every file up to 2 GB.
- Text ASS, SBV, VTT, MicroDVD/SubViewer SUB, and Spruce STL are also accepted, with the existing **1 MB / 3,000-cue** limits. Binary VobSub and EBU STL are not supported. The SUB bridge restores original frames/timestamps, and ASS restores source styles, so these formats can require metadata restoration rather than byte-identical website output.
- A status message shows processing stages, not a translation percentage. One browser runs at a time, with at most two jobs waiting. Each translation gets a fresh browser and isolated context, which are closed after the download or a failure; the batch and queue remain in the bot process. This does not change the server's IP address or prevent the website from recognizing or rate-limiting requests. Opening the site may be retried before uploading, but a failed upload is reported rather than repeated. Website wait timeouts are renewed while cues or indicators keep changing. Logs contain only sanitized categories and counts, not subtitle text or credentials.

**Privacy:** Subtitles are sent to the website and its Google Translate widget. Do not send confidential files, and translate only files you have permission to use. See the site's [privacy policy](https://translatesubtitles.co/privacypolicy.php).

## Telegram transfers and practical limits

The public Telegram Bot API downloads up to 20 MB and uploads up to 50 MB. To transfer larger SRTs, configure **both** `API_ID` and `API_HASH` as well as `BOT_TOKEN`. The bot then authenticates to Telegram MTProto **as the same bot**, with an in-memory session, and uses disk-backed transfers. It does not need a phone number or a user-account login. `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` and `TG_API_ID` / `TG_API_HASH` are accepted aliases. Without both credentials, smaller files continue to use the normal Bot API; larger transfers report an error.

Telegram still imposes its own bot file-size limits (approximately 2 GB), and a translated file may be larger than its input. MTProto transfers have automated **mock** tests but have not been verified against your Telegram account or Koyeb deployment.

The deployment needs enough **temporary disk for the source, the website's downloaded result, and browser overhead at the same time**. If UTF-16 or a legacy encoding needs conversion, that can require additional space. Koyeb's [free instance](https://www.koyeb.com/docs/reference/instances#free-instances) has 512 MB RAM, 0.1 vCPU and 2 GB SSD; it cannot be assumed to accommodate a 2 GB input plus its output, and the website's full-file browser processing can exhaust RAM well before then. A larger instance/disk may be needed. Jobs can also fail or be interrupted by website outages, browser crashes, disk exhaustion, Telegram limits, scale-down, or deployment restarts. The bot does not intentionally send a detected partial download as a complete translation.

## Deploy on Koyeb

1. Create a bot with [@BotFather](https://t.me/BotFather). Keep its token and API credentials private; do not paste them into chat or commit them.
2. In Koyeb, create a **Web Service** from the GitHub branch containing this code, select the **Dockerfile** builder, and expose HTTP port `8080`. The Dockerfile installs Chromium.
3. Set these environment variables in Koyeb, using Secrets for sensitive values:

   | Variable | Purpose |
   | --- | --- |
   | `BOT_TOKEN` | BotFather token |
   | `API_ID` and `API_HASH` | Telegram API credentials for transfers beyond public Bot API limits; optional for smaller files |
   | `PORT` | `8080`, or the port configured for the service |

   Webhook authentication is derived from the bot token; no separate webhook secret is required. The old `TELEGRAM_WEBHOOK_SECRET` setting is ignored.
4. Deploy. The bot derives its public webhook URL from `KOYEB_PUBLIC_DOMAIN`. Look for **Telegram webhook registered** in the logs. `GET /healthz` returns HTTP 200 while the service is alive, including during Telegram retries; `GET /readyz` returns HTTP 200 only once the webhook is registered. Use `/healthz` for the Koyeb liveness check.
5. In Telegram, send `/start`, upload a small `.srt`, and choose a language. For a private bot, send `/id`, then optionally set `ALLOWED_USER_IDS` to your numeric ID and redeploy.

**Check pricing and billing** before choosing a larger instance or additional storage. The free instance is a hobby deployment, not a durable queue. The queue and progress are in memory. A restart, crash or scale-down can interrupt a job; retry by tapping the language button or resending the document. Best-effort health requests during an active job reduce idle scale-down but do not guarantee uninterrupted processing.

### Optional settings

| Variable | Default | Description |
| --- | --- | --- |
| `WEBHOOK_BASE_URL` | Derived from `KOYEB_PUBLIC_DOMAIN` | Public HTTPS origin without a path, e.g. `https://bot.example.com`. |
| `ALLOWED_USER_IDS` | Unset | Comma-separated Telegram user IDs; unset allows private-chat users. |
| `TRANSLATION_TIMEOUT_SECONDS` | `900` | Website translation inactivity timeout, 60–1,000 seconds; renewed when cue text or indicators change. Some other site operations have separate timeouts. There is no fixed total deadline for an entire SRT or Select-all batch. |
| `CHROMIUM_PATH` | Docker: `/usr/bin/chromium` | Local Chromium executable; if unset, Playwright uses its installed browser. |
| `WEBSITE_PROXIES` | Unset | Optional comma-separated pool of up to 10 proxies for the website browser only. Store it as a Koyeb Secret; see below. |
| `XRAY_PATH` | `xray` on PATH | Xray executable used for VLESS links. Included in the Docker image. |

### Optional website proxy pool

Set `WEBSITE_PROXIES` to proxies you trust, for example `http://user:password@proxy.example:8080,socks5://proxy2.example:1080`. Supported schemes are HTTP, HTTPS, SOCKS4, SOCKS5 and supported VLESS links; a bare `host:port` is treated as HTTP. Chromium does not support SOCKS authentication here; use an unauthenticated SOCKS endpoint or an authenticated HTTP proxy. Percent-encode special characters in usernames and passwords, including commas. Never commit or paste actual credentials into chat.

To configure a temporary bot-wide pool from Telegram, first set `ALLOWED_USER_IDS` and redeploy. Every user in that allowlist can change the pool for everyone. In a private chat, send `/proxychk`, then send a UTF-8 text file (up to 16 KiB) or a plain-text list of up to 10 URLs, including supported VLESS links, one per line or comma-separated. The bot checks each and replaces the pool only with passing entries; if none pass, the old pool remains. Use `/proxychk direct` (or send `direct` after `/proxychk`) to disable proxies, and `/proxychk cancel` to cancel input. The check waits for an active batch to finish. Changes are in memory only; a restart restores `WEBSITE_PROXIES` from the environment (or direct if unset). Telegram bot chats are not end-to-end encrypted, so prefer the Koyeb Secret for sensitive credentials.

Before each translation, the bot tests proxies in rotation by opening the site's upload page in a disposable browser context, without uploading a subtitle. It uses the first one that passes for the whole translation, and tests again for the next translation. If none pass, the job fails without uploading or falling back to a direct connection. This check only confirms the upload page was reachable, not that Google Translate or the eventual translation will work. The Telegram connection does not use this pool. Slow proxies add startup time (up to 20 seconds per check).

For VLESS links the Docker image includes a pinned Xray binary. The bot starts a temporary local-only SOCKS bridge for each check or translation and stops it afterward; its configuration file is private and temporary. Non-Docker installations must install a compatible `xray` executable on PATH (or set `XRAY_PATH`). Supported VLESS links use a UUID, `encryption=none`, TCP/raw, WebSocket or gRPC, and `security=none`, `tls`, or `reality` (Reality requires `sni` and `pbk`). Common `flow`, TLS and transport parameters are supported; unsupported options and transports are rejected, not silently ignored. TLS certificate verification is always enabled. `security=none` does not encrypt the VLESS connection. Check your provider's configuration and use a trusted endpoint; not every VLESS link is supported or guaranteed to work. Xray consumes additional memory on top of Chromium.

A proxy operator can observe your traffic and may be able to inspect subtitle data, especially if they control TLS or the destination. Use only providers you trust. Proxies do not guarantee anonymity or prevent site tracking, rate limits, outages, or failed translations; respect the website's terms.

## Development and testing

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
TEST_CHROMIUM_PATH=/usr/bin/chromium python -m pytest -q
```

The browser tests use a **local mock**, including cases where a success message appears while an animation is still running and later subtitle text changes. The suite tests disk-backed validation above the previous SRT size and cue-count caps, incomplete downloads, cancellation and mocked MTProto authentication/transfers. These are not a real Telegram or Koyeb test.

CI builds the deployment image and runs a mocked Telegram → real Chromium → Telegram flow for a 3,000-cue, 999,786-byte SRT under a 512,000,000-byte memory limit, no swap and 0.1 CPU. The mock-only settling wait is shortened; production waits longer after the animation finishes. Reproduce locally with Docker:

```sh
docker build -t subtranslate-local .
docker run --rm --network none --memory 512000000 --memory-swap 512000000 --cpus 0.1 \
  --mount type=bind,source="$PWD/tests",target=/smoke,readonly \
  --env PYTHONPATH=/app subtranslate-local python /smoke/resource_smoke.py
```

To test the **actual website** from a machine with network access, install Chromium or run `python -m playwright install chromium`:

```sh
python -m subtranslate.smoke tests/fixtures/sample.srt hi --output-dir .
```

The command refuses to overwrite an existing file. An opt-in **Live website smoke test** runs small public samples and two independent synthetic 405-cue direct-download jobs under Koyeb-like limits, comparing the adaptive wait with a 120-second quiet wait. To request it from an unmerged branch, include `[live-site-test]` in the pushed commit message; alternatively use workflow dispatch once the workflow exists on the default branch. It does not use your Telegram bot or Koyeb deployment. Site outages and rate limits can cause failures.

## Troubleshooting

- **No response:** allow for cold start, check `/readyz` and look for **Telegram webhook registered**. Ensure no other deployment is changing the webhook for the same bot.
- **Telegram startup times out:** `/healthz` remains live while initialization retries. If retries persist, check Koyeb DNS and outbound access to `api.telegram.org`; a timeout does not prove the token is wrong. Never share the token.
- **Large transfer unavailable:** check that both API ID and hash exist in Koyeb under one of the supported variable-name pairs, and that the deployment can reach Telegram. The bot does not print those values.
- **Website upload, parsing, or translation error:** the website can change, time out, block automation, or run out of browser resources. Failures before upload may be retried with a fresh browser; after an upload starts, a failure is reported without reuploading. The sanitized log's stage, error category, and browser state are clues, not proof of the cause. Try again later; no detected incomplete result is sent.
- **Disk or memory exhaustion:** free-instance resources may be insufficient for the whole source, website processing and result. Consider a larger instance or disk after checking cost; there is no automatic resume.
- **Unexpected unchanged text:** a long unchanged run is rejected as potentially partial, although it can be legitimate. The detection is not a full language or quality check and cannot establish that every cue was translated correctly.

The selector flow was informed by [MutennRoshii/subtitle-translator](https://github.com/MutennRoshii/subtitle-translator); see `THIRD_PARTY_NOTICES.md`.
