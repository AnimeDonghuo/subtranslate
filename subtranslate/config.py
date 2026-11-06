"""Configuration for a single, publicly reachable webhook service."""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    webhook_base_url: str
    port: int = 8080
    chromium_path: str | None = None
    translation_timeout_seconds: int = 900
    allowed_user_ids: frozenset[int] | None = None

    @property
    def webhook_url(self) -> str:
        return self.webhook_base_url + "/telegram/webhook"

    @property
    def webhook_secret(self) -> str:
        # Generate a separate Telegram-compatible value from the bot token.
        # It stays stable across restarts and the bot token itself is never
        # sent in the webhook authentication header.
        return hmac.new(
            self.bot_token.encode("utf-8"), b"subtranslate/telegram-webhook/v1", hashlib.sha256
        ).hexdigest()

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN is required")
        base = os.getenv("WEBHOOK_BASE_URL", "").strip()
        if not base:
            koyeb_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()
            if koyeb_domain:
                base = f"https://{koyeb_domain}"
        base = base.rstrip("/")
        try:
            parsed = urlsplit(base)
            # The listener is mounted at the root, so accepting a path here
            # would register a URL that can never reach the bot.
            valid_base = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or parsed.port in (443, 80, 88, 8443))
            )
        except ValueError:
            valid_base = False
        if not valid_base:
            raise ValueError("Set WEBHOOK_BASE_URL to a public HTTPS origin, or use Koyeb's KOYEB_PUBLIC_DOMAIN")

        try:
            port = int(os.getenv("PORT", "8080"))
            timeout = int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "900"))
            ids = os.getenv("ALLOWED_USER_IDS", "").strip()
            allowed = frozenset(int(value.strip()) for value in ids.split(",")) if ids else None
        except ValueError as exc:
            raise ValueError("PORT, TRANSLATION_TIMEOUT_SECONDS and ALLOWED_USER_IDS must be integers") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        # A deadline applies to each individual language, not to a whole
        # all-language batch, which may take much longer.
        if not 60 <= timeout <= 1000:
            raise ValueError("TRANSLATION_TIMEOUT_SECONDS must be between 60 and 1000")
        if allowed is not None and (not allowed or any(value <= 0 for value in allowed)):
            raise ValueError("ALLOWED_USER_IDS must contain positive Telegram user IDs")
        return cls(
            bot_token=token,
            webhook_base_url=base,
            port=port,
            chromium_path=os.getenv("CHROMIUM_PATH", "").strip() or None,
            translation_timeout_seconds=timeout,
            allowed_user_ids=allowed,
        )
