# SubTranslate Telegram bot

Send a subtitle as a Telegram document, choose a language, and receive the website's downloaded file, for example `movie.Hindi.srt`. The bot automates a public subtitle-translation website (its URL lives in `subtranslate/website.py`); it does not use a different translation service. **Bot messages never name that website**, so users only see "the translation service" or "the website".

## How it works

- Use a **private chat**. The language picker has five buttons per page. Choose one language, or confirm **Select all languages** to translate and send one file at a time, sequentially. `/status` shows each running or queued task with its stage bar, `/cancel_id <n>` stops one of them, and `/cancel` removes your queued jobs and stops active work.
- For an SRT, the bot uploads the **whole file once**, selects the language, clicks Translate, waits for the visible website/Google loading animations to finish and for text changes to settle, and clicks **Download**. If a long preview section appears unchanged (including when no cues have changed), it waits longer before that one download. This can help with delayed translations, but cannot fix a failure of the website or Google Translate. It sends the single downloaded SRT **without assembling parts or rewriting its bytes**. The on-page preview is not the delivered file.
- The bot checks cue count and timestamps, and refuses a download with a strong sign of missing translation or a long unchanged section mixed with changed text. An entirely unchanged result is flagged because it might already be in the selected language. These are heuristics: they can reject legitimate unchanged sections, and same-script partial translations or incorrect words can still escape detection. Always review machine translation; completeness and quality cannot be guaranteed.
- SRT files have **no bot-imposed total byte-size or cue-count cap**. Input and downloaded files are validated incrementally on temporary disk; the browser and website still process the original file as a single upload. A single cue must fit available memory. There is no guarantee that the site, browser, Telegram, or a particular host can process every file up to 2 GB.
- Text ASS, SBV, VTT, MicroDVD/SubViewer SUB, and Spruce STL are also accepted, with the existing **1 MB / 3,000-cue** limits. Binary VobSub and EBU STL are not supported. The SUB bridge restores original frames/timestamps, and ASS restores source styles, so these formats can require metadata restoration rather than byte-identical website output.
- A status message shows processing stages, not a translation percentage. One browser runs at a time, with at most two jobs waiting. Each translation gets a fresh browser and isolated context, which are closed after the download or a failure; the batch and queue remain in the bot process. This does not change the server's IP address or prevent the website from recognizing or rate-limiting requests. Opening the site may be retried before uploading, but a failed upload is reported rather than repeated. Website wait timeouts are renewed while cues or indicators keep changing. Logs contain only sanitized categories and counts, not subtitle text or credentials.

**Privacy:** Subtitles are sent to the website and its Google Translate widget. Do not send confidential files, and translate only files you have permission to use. The site's privacy policy is linked in its footer. The bot keeps a copy of every delivered file in the optional log channel (see below).

## Telegram transfers and practical limits

The public Telegram Bot API downloads up to 20 MB and uploads up to 50 MB. To bypass those limits, configure **both** `API_ID` and `API_HASH` as well as `BOT_TOKEN`. The bot then authenticates to Telegram MTProto **as the same bot**, with an in-memory session, and uses disk-backed transfers above the Bot API thresholds — up to Telegram's own bot cap of **about 2 GB per file**. It does not need a phone number or a user-account login. `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` and `TG_API_ID` / `TG_API_HASH` are accepted aliases. Without both credentials, smaller files continue to use the normal Bot API and larger transfers report an error. Files above 2 GB are refused before any upload with a clear message, and the startup log states whether the large-file path is enabled. The log channel copy uses the same MTProto path for large files.

Telegram still imposes its own bot file-size limits (approximately 2 GB), and a translated file may be larger than its input. MTProto transfers have automated **mock** tests but have not been verified against your Telegram account or Koyeb deployment.

The deployment needs enough **temporary disk for the source, the website's downloaded result, and browser overhead at the same time**. If UTF-16 or a legacy encoding needs conversion, that can require additional space. Koyeb's [free instance](https://www.koyeb.com/docs/reference/instances#free-instances) has 512 MB RAM, 0.1 vCPU and 2 GB SSD; it cannot be assumed to accommodate a 2 GB input plus its output, and the website's full-file browser processing can exhaust RAM well before then. A larger instance/disk may be needed. Jobs can also fail or be interrupted by website outages, browser crashes, disk exhaustion, Telegram limits, scale-down, or deployment restarts. The bot does not intentionally send a detected partial download as a complete translation.

## Deploy on Koyeb or Railway

1. Create a bot with [@BotFather](https://t.me/BotFather). Keep its token and API credentials private; do not paste them into chat or commit them.
2. In Koyeb or Railway, deploy this repository. On Koyeb, select the **Dockerfile** builder and expose HTTP port `8080`. On Railway, the Dockerfile is auto-detected and `PORT` is automatically configured.
3. Set these environment variables:

   | Variable | Purpose |
   | --- | --- |
   | `BOT_TOKEN` | BotFather token |
   | `OWNER_ID` | Your numeric Telegram user ID (comma-separated for several owners). Owners always have access, cannot be banned, and are the only users who can run `/ban`, `/auth`, `/public`, `/private` and `/stats` |
   | `API_ID` and `API_HASH` | Telegram API credentials for transfers beyond public Bot API limits; optional for smaller files |
   | `PORT` | `8080` on Koyeb, or dynamically injected by Railway |
   | `MONGODB_URI` | Optional MongoDB connection string (`mongodb+srv://...`). Stores presets, bans, access rules and usage counters |
   | `LOG_CHANNEL_ID` | Optional channel ID (e.g. `-1001234567890`) that receives a copy of every delivered subtitle with the sender's ID, name and username |

   Webhook authentication is derived from the bot token; no separate webhook secret is required. The bot automatically derives its public webhook URL from `KOYEB_PUBLIC_DOMAIN` on Koyeb or `RAILWAY_PUBLIC_DOMAIN` / `RAILWAY_STATIC_URL` on Railway.
4. Deploy. Look for **Telegram webhook registered** in the logs. `GET /healthz` returns HTTP 200 while the service is alive, including during Telegram retries; `GET /readyz` returns HTTP 200 only once the webhook is registered. Use `/healthz` for health checks.
5. In Telegram, send `/start`, upload a small `.srt`, and choose a language. For a private bot, send `/id`, then optionally set `ALLOWED_USER_IDS` to your numeric ID and redeploy.

**Check pricing and billing** before choosing a larger instance or additional storage. The free instance is a hobby deployment, not a durable queue. The queue and progress are in memory. A restart, crash or scale-down can interrupt a job; retry by tapping the language button or resending the document. Best-effort health requests during an active job reduce idle scale-down but do not guarantee uninterrupted processing.

### Optional settings

| Variable | Default | Description |
| --- | --- | --- |
| `WEBHOOK_BASE_URL` | Derived from `KOYEB_PUBLIC_DOMAIN` / `RAILWAY_PUBLIC_DOMAIN` | Public HTTPS origin without a path, e.g. `https://bot.example.com`. |
| `ALLOWED_USER_IDS` | Unset | Comma-separated Telegram user IDs; unset allows private-chat users. |
| `TRANSLATION_TIMEOUT_SECONDS` | `900` | Website translation inactivity timeout, 60–1,000 seconds; renewed when cue text or indicators change. Some other site operations have separate timeouts. There is no fixed total deadline for an entire SRT or Select-all batch. |
| `CHROMIUM_PATH` | Docker: `/usr/bin/chromium` | Local Chromium executable; if unset, Playwright uses its installed browser. |
| `WEBSITE_PROXIES` | Unset | Optional comma-separated pool of up to 10 proxies for the website browser only. Store it as a Koyeb Secret; see below. |
| `XRAY_PATH` | `xray` on PATH | Xray executable used for VLESS links. Included in the Docker image. |
| `OWNER_IDS` | Unset | Alias for `OWNER_ID`; comma-separated numeric IDs. |
| `MONGODB_DB` | `subtranslate` | Database name (letters, digits, `_` and `-`, up to 64 characters). |
| `MONGODB_STORAGE_LIMIT_MB` | `512` | Plan quota reported as free space by `/stats`; `0` uses the server's own volume figures. |
| `LOG_CHANNEL_ID` | Unset | Channel that receives a copy of each delivered subtitle. Add the bot as an administrator with "Post messages". |

### Optional website proxy pool

Set `WEBSITE_PROXIES` to proxies you trust, for example `http://user:password@proxy.example:8080,socks5://proxy2.example:1080`. Supported schemes are HTTP, HTTPS, SOCKS4, SOCKS5 and supported VLESS links; a bare `host:port` is treated as HTTP. Chromium does not support SOCKS authentication here; use an unauthenticated SOCKS endpoint or an authenticated HTTP proxy. Percent-encode special characters in usernames and passwords, including commas. Never commit or paste actual credentials into chat.

To configure a temporary bot-wide pool from Telegram, first set `ALLOWED_USER_IDS` and redeploy. Every user in that allowlist can change the pool for everyone. In a private chat, send `/proxychk`, then send a UTF-8 text file (up to 19 MiB) or plain-text list of up to 80,000 URLs, including supported VLESS links, one per line or comma-separated. Only test proxies you are authorized to use. Invalid entries are skipped; the background scan checks all valid entries and replaces the pool only with passing entries when it finishes. If none pass or the scan is canceled, the old pool remains. Use `/proxychk status` for counts, `/proxychk cancel` to cancel, or `/proxychk direct` (or send `direct` after `/proxychk`) to cancel the scan and disable proxies. Telegram bot chats are not end-to-end encrypted, so prefer the Koyeb Secret for sensitive credentials.

Checks run sequentially in small batches so translations can use the browser between batches. A large list can take many hours or days, particularly with slow or dead proxies. The scan, its progress, and the resulting pool are in memory only; a restart or scale-down interrupts the scan and restores `WEBSITE_PROXIES` from the environment (or direct if unset). Best-effort health pings do not guarantee uninterrupted processing. Input above 19 MiB or 80,000 entries must be split; some 80,000-entry VLESS lists may exceed the size limit. The Telegram Bot API limits file downloads to 20 MB.

Before each translation, the bot tests proxies in rotation by opening the site's upload page in a disposable browser context, without uploading a subtitle. It uses the first one that passes for the whole translation, and tests again for the next translation. For each translation, it tries at most 10 pool entries; if none pass, the job fails without uploading or falling back to a direct connection. A later job starts checking from the next entry. This check only confirms the upload page was reachable, not that Google Translate or the eventual translation will work. The Telegram connection does not use this pool. Slow proxies add startup time (up to 20 seconds per check).

For VLESS links the Docker image includes a pinned Xray binary. The bot starts a temporary local-only SOCKS bridge for each check or translation and stops it afterward; its configuration file is private and temporary. Non-Docker installations must install a compatible `xray` executable on PATH (or set `XRAY_PATH`). Supported VLESS links use a UUID, `encryption=none`, TCP/raw, WebSocket, gRPC or XHTTP, and `security=none`, `tls`, or `reality` (Reality requires `sni` and `pbk`). Common `flow`, TLS and transport parameters are supported; unsupported options and transports are rejected, not silently ignored. TLS certificate verification is always enabled. `security=none` does not encrypt the VLESS connection. Check your provider's configuration and use a trusted endpoint; not every VLESS link is supported or guaranteed to work. Xray consumes additional memory on top of Chromium.

A proxy operator can observe your traffic and may be able to inspect subtitle data, especially if they control TLS or the destination. Use only providers you trust. Proxies do not guarantee anonymity or prevent site tracking, rate limits, outages, or failed translations; respect the website's terms.

### Free-tier capacity and acceptable use

The bot is built to run on **Koyeb's free 512 MB instance** and Railway's free trial:

- **One web service, one port.** It listens on `PORT` and uses Telegram's webhook, so there is no long-lived polling connection; `/healthz` is the health route Koyeb probes.
- **No volumes.** Each job gets its own temporary directory under the default temp area; the source, the downloaded result and the browser's temporary files are removed when the job ends or is cancelled.
- **Memory-aware queue.** `MAX_CONCURRENT_JOBS=2`, but before a second job starts the bot reads the container's **own cgroup limit** (`/sys/fs/cgroup/memory.max` + `memory.current`, with the cgroup v1 files as fallback) and the host's `MemAvailable`, and uses the smaller figure. Below `MIN_FREE_MB` (default 250) the extra job stays in the queue, so a free instance falls back to one job instead of being OOM-killed. The Playwright session is serialised anyway, so two browsers never run at once.
- **Sleep and cold starts.** The webhook is registered on boot, HTTP timeouts are raised to 30 s, and a long job pings `/healthz` so Koyeb/Railway see traffic while it runs.
- **MongoDB is optional.** Without it, settings live in memory; Atlas's free M0 tier is enough for presets, bans, access and counters.

**Acceptable use.** The bot only translates subtitle files that users send it. It does not mine cryptocurrency, torrent, relay third-party traffic, host or stream media, or resell platform capacity. Two optional features deserve your own judgement:

- `WEBSITE_PROXIES` (including the bundled VLESS/Xray bridge) routes only the bot's own browser requests to the translation website. Railway's terms restrict running a proxy or VPN service from their network; if that applies to you, leave `WEBSITE_PROXIES` unset — translation works without it.
- Translating subtitles you have no permission to translate can infringe copyright; users are warned, and `/ban` is available.

You remain responsible for the files your users send.

## Users, settings and moderation

The bot supports many users at once. Settings and moderation state live in **MongoDB** when `MONGODB_URI` is set, and in memory otherwise. Either way the bot starts and translates: if the database is unreachable it logs a warning, keeps working, and `/stats` shows `in-memory (MongoDB not connected)`. Nothing about the translation flow depends on the database.

`MONGODB_URI` works with a free **Atlas** cluster (M0, 512 MB) from Railway or Koyeb; the database is outbound traffic only, so no extra port or container is needed. Create the cluster, add a database user, allow your host's IPs (`0.0.0.0/0` for a hobby deployment), and paste the `mongodb+srv://` string into Railway/Koyeb as a secret.

| Command | Who | What it does |
| --- | --- | --- |
| `/start` | everyone | Welcome screen with the rules, the permission/18+ warning and shortcut buttons |
| `/help` | everyone | Interactive help: every topic opens from a button in the same message |
| `/ussetting` | everyone | Personal settings. **Preset languages**: select any number of languages (selected ones show ✅), then **💾 Save**. Once a user has saved presets, the language picker shows **one extra green button — ⭐ Saved languages to use (N)** — which translates that whole set one file at a time, exactly like the normal language buttons |
| `/status` | everyone | Opens one **live** message per chat that refreshes itself every 6 seconds while work is running, with a 🔄 Refresh button and a 🛑 Cancel #N button per task (cancelling stops a download, upload or website stage). Asking `/status` again deletes your old message and shows a new one; other users' messages are never touched. It appears automatically when a task is queued. Each task is a numbered block: `■□` **stage** bar (not the website's translation percentage), `Step n/10`, the current stage, elapsed time and the sender's name, ID and @username. A batch also shows how many languages are done. Owners see every task; everyone else sees only their own |
| `/cancel_id <n>` | everyone | Stops exactly one task by its `/status` number — a single translation, a whole multi-language batch, or an in-flight download/upload — without touching other tasks. The same action is on each live-status 🛑 button. Owners may stop anyone's task; a user may only stop their own |
| `/broadcast <text>` | owner | Sends one plain-text message to every known user (or use it as a reply to a message), skipping banned users and reporting sent/failed/skipped counts |
| `/stats` | everyone | Owners see users, active users, bans, authorized users, mode, translations (today/week/month/year) and MongoDB space used/free; other users see their own totals and monthly rank |
| `/leaderboard` | everyone | Top 10 users by translated subtitles with **Today / Week / Month / Year** buttons, plus the caller's rank |
| `/public` | owner | Anyone in a private chat may use the bot (banned users still cannot) |
| `/private` | owner | Only owners, `ALLOWED_USER_IDS` and `/auth`-ed users may use the bot |
| `/auth <id>` · `/unauth <id>` | owner | Add or remove an authorized user |
| `/authlist` | owner | Everyone who can use the bot in one place: owners, `ALLOWED_USER_IDS` and `/auth` entries, each with name, `@username` and ID, plus the current access mode |
| `/addpremium <id> <days>` | owner | Gives that user premium for N days (default 30, max 3650): unlimited preset languages and the 🌐 Select all languages button. The user is notified |
| `/removepremium <id>` | owner | Ends premium immediately; the user drops back to 10 presets and the 🔒 locked select-all button |
| `/ban <id>` · `/release <id>` | owner | Block a user everywhere until released. A ban always applies, in public and private mode |
| `/banlist` | owner | Banned users as `name (@username) · id` with the ban date, newest first |

**Free limits.** Owners and premium users have no limits. Everyone else can save **10 preset languages** (`/ussetting` shows `7/10 free slots used`), and their language picker shows `🔒 Select all languages · premium` instead of the working button — tapping it explains how to ask the owner. Premium is stored per user in MongoDB with an expiry date, so it survives restarts. Owners are always premium.

**Concurrency and flood control.** `MAX_CONCURRENT_JOBS` (default 2) jobs run at once; extra jobs wait in the queue and `/status` lists them. Before a second worker takes a job it checks `MemAvailable`: below `MIN_FREE_MB` (default 250) it keeps the extra work queued, so a 512 MB Koyeb/Railway instance degrades to one job instead of being killed. The bot also uses PTB's `AIORateLimiter` (about 20 messages/second) which pauses every request during a Telegram `RetryAfter` flood wait, and `/broadcast` sleeps out the wait and retries instead of dropping users.

**Button colours.** Keyboards use the Bot API 9.4 `style` field: **green** for a recommended or positive action (how to translate, save, use saved languages), **blue** for normal choices (language buttons, selecting presets), and **red** for heavy or destructive actions (select all languages, clear presets, owner tools). Clients older than February 2026 simply show them unstyled.

Defaults stay compatible with earlier releases: with no `ALLOWED_USER_IDS` the bot is **public**, and with `ALLOWED_USER_IDS` set it is **private**. The mode chosen with `/public` or `/private` is stored in MongoDB and survives restarts.

**Log channel.** Set `LOG_CHANNEL_ID` and promote the bot to administrator in that channel. After each successful delivery the bot posts the translated file with a caption containing the sender's name, `@username` (or "not set"), numeric ID, target language, original and delivered filename, and cue count. If the channel refuses the copy (missing rights, oversized file), the delivery to the user still succeeds and the failure is logged.

**Practical limits.** Usage counters are one small document per delivered subtitle. `/leaderboard` and `/stats` use indexed queries, so they stay fast on a free Atlas cluster; the 512 MB quota is usually reached by years of heavy use, not weeks. Without MongoDB, counters are kept in memory only and reset on restart.

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
- **MongoDB warnings:** `MongoDB unavailable` means the bot fell back to in-memory settings — translations still work, but bans/presets/access mode reset on restart. Check the connection string, the database user and the Atlas IP allowlist. The string is never logged.
- **Unexpected unchanged text:** a long unchanged run is rejected as potentially partial, although it can be legitimate. The detection is not a full language or quality check and cannot establish that every cue was translated correctly.

The selector flow was informed by [MutennRoshii/subtitle-translator](https://github.com/MutennRoshii/subtitle-translator); see `THIRD_PARTY_NOTICES.md`.
