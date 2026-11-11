"""Configuration for a single, publicly reachable webhook service."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .proxies import BrowserProxy, VlessProxy, parse_proxy_pool


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    webhook_base_url: str
    port: int = 8080
    chromium_path: str | None = None
    translation_timeout_seconds: int = 900
    allowed_user_ids: frozenset[int] | None = None
    # Owners always have access, can never be banned, and are the only users
    # allowed to change access rules, bans and other users' authorization.
    owner_ids: frozenset[int] = frozenset()
    # Optional MongoDB persistence for per-user presets, bans, access rules
    # and usage counters. Without it the bot keeps an in-memory copy.
    mongodb_uri: str = field(default="", repr=False)
    mongodb_database: str = "subtranslate"
    mongodb_storage_limit_mb: int = 512
    # Optional channel that receives a copy of every delivered subtitle.
    log_channel_id: int | None = None
    # Optional MTProto credentials are used only for transfers beyond the
    # public Bot API's file limits. Never include either value in repr/logs.
    api_id: int | None = field(default=None, repr=False)
    api_hash: str | None = field(default=None, repr=False)
    website_proxies: tuple[BrowserProxy | VlessProxy, ...] = field(default=(), repr=False)

    @property
    def default_access_mode(self) -> str:
        """Keep the historic behaviour when no runtime mode is stored yet."""
        return "private" if self.allowed_user_ids else "public"

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
            else:
                railway_domain = (
                    os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
                    or os.getenv("RAILWAY_STATIC_URL", "").strip()
                )
                if railway_domain:
                    clean_domain = railway_domain.removeprefix("https://").removeprefix("http://").rstrip("/")
                    base = f"https://{clean_domain}"
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
            raise ValueError("Set WEBHOOK_BASE_URL to a public HTTPS origin, or use Koyeb's KOYEB_PUBLIC_DOMAIN or Railway's RAILWAY_PUBLIC_DOMAIN")

        try:
            port = int(os.getenv("PORT", "8080"))
            timeout = int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "900"))
            ids = os.getenv("ALLOWED_USER_IDS", "").strip()
            allowed = frozenset(int(value.strip()) for value in ids.split(",")) if ids else None
        except ValueError as exc:
            raise ValueError("PORT, TRANSLATION_TIMEOUT_SECONDS and ALLOWED_USER_IDS must be integers") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        # The timeout bounds website waits, not the entire file or batch.
        if not 60 <= timeout <= 1000:
            raise ValueError("TRANSLATION_TIMEOUT_SECONDS must be between 60 and 1000")
        if allowed is not None and (not allowed or any(value <= 0 for value in allowed)):
            raise ValueError("ALLOWED_USER_IDS must contain positive Telegram user IDs")
        # Common environment names are accepted without ever logging their
        # values. A missing half disables large transfers, not the webhook.
        def first_nonempty(*names: str) -> str:
            return next((value for name in names if (value := os.getenv(name, "").strip())), "")

        owner_text = first_nonempty("OWNER_ID", "OWNER_IDS")
        try:
            owners = frozenset(
                int(value) for value in owner_text.replace(" ", "").split(",") if value
            )
        except ValueError:
            raise ValueError("OWNER_ID must contain numeric Telegram user IDs") from None
        if any(value <= 0 for value in owners):
            raise ValueError("OWNER_ID must contain positive Telegram user IDs")

        log_channel_text = first_nonempty("LOG_CHANNEL_ID")
        try:
            log_channel_id = int(log_channel_text) if log_channel_text else None
            if log_channel_id == 0:
                raise ValueError
        except ValueError:
            raise ValueError("LOG_CHANNEL_ID must be an integer Telegram chat ID") from None

        database_name = os.getenv("MONGODB_DB", "subtranslate").strip() or "subtranslate"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", database_name):
            raise ValueError("MONGODB_DB must be 1-64 letters, digits, underscores or hyphens")
        try:
            storage_limit = int(os.getenv("MONGODB_STORAGE_LIMIT_MB", "512"))
        except ValueError:
            raise ValueError("MONGODB_STORAGE_LIMIT_MB must be an integer") from None
        if not 0 <= storage_limit <= 1_000_000:
            raise ValueError("MONGODB_STORAGE_LIMIT_MB must be between 0 and 1,000,000")

        api_id_text = first_nonempty("API_ID", "TELEGRAM_API_ID", "TG_API_ID")
        api_hash = first_nonempty("API_HASH", "TELEGRAM_API_HASH", "TG_API_HASH")
        try:
            api_id = int(api_id_text) if api_id_text else None
            if api_id is not None and api_id <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("API_ID (or TELEGRAM_API_ID) must be a positive integer") from None
        return cls(
            bot_token=token,
            webhook_base_url=base,
            port=port,
            chromium_path=os.getenv("CHROMIUM_PATH", "").strip() or None,
            translation_timeout_seconds=timeout,
            allowed_user_ids=allowed,
            owner_ids=owners,
            mongodb_uri=os.getenv("MONGODB_URI", "").strip(),
            mongodb_database=database_name,
            mongodb_storage_limit_mb=storage_limit,
            log_channel_id=log_channel_id,
            api_id=api_id,
            api_hash=api_hash or None,
            website_proxies=parse_proxy_pool(os.getenv("WEBSITE_PROXIES", "")),
        )
