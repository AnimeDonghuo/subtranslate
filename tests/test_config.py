import pytest

from subtranslate.config import Settings


@pytest.fixture
def environment(monkeypatch):
    for key in ("BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "WEBHOOK_BASE_URL", "KOYEB_PUBLIC_DOMAIN",
                "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL", "PORT", "ALLOWED_USER_IDS",
                "OWNER_ID", "OWNER_IDS", "LOG_CHANNEL_ID", "MONGODB_URI", "MONGODB_DB",
                "MONGODB_STORAGE_LIMIT_MB", "TRANSLATION_TIMEOUT_SECONDS", "CHROMIUM_PATH",
                "API_ID", "API_HASH", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TG_API_ID",
                "TG_API_HASH", "WEBSITE_PROXIES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:example")
    monkeypatch.setenv("KOYEB_PUBLIC_DOMAIN", "demo-example.koyeb.app")


def test_koyeb_domain_and_allowlist(environment, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "123, 456")
    settings = Settings.from_env()
    assert settings.webhook_url == "https://demo-example.koyeb.app/telegram/webhook"
    assert settings.allowed_user_ids == frozenset({123, 456})
    assert settings.port == 8080


def test_railway_domain(environment, monkeypatch):
    monkeypatch.delenv("KOYEB_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "subtranslate.up.railway.app")
    settings = Settings.from_env()
    assert settings.webhook_url == "https://subtranslate.up.railway.app/telegram/webhook"

    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setenv("RAILWAY_STATIC_URL", "https://subtranslate-static.up.railway.app")
    settings2 = Settings.from_env()
    assert settings2.webhook_url == "https://subtranslate-static.up.railway.app/telegram/webhook"


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/foo", "https://user:pass@example.com", "https://example.com:9999", "https://example.com?x=y"])
def test_rejects_unusable_public_origins(environment, monkeypatch, url):
    monkeypatch.setenv("WEBHOOK_BASE_URL", url)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_webhook_secret_is_derived_stably_from_bot_token(environment, monkeypatch):
    import re

    first = Settings.from_env()
    second = Settings.from_env()
    assert first.webhook_secret == second.webhook_secret
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,256}", first.webhook_secret)
    assert first.bot_token not in first.webhook_secret and first.bot_token not in repr(first)
    monkeypatch.setenv("BOT_TOKEN", "123456:different")
    assert Settings.from_env().webhook_secret != first.webhook_secret


def test_old_manual_secret_variable_is_ignored(environment, monkeypatch):
    expected = Settings.from_env().webhook_secret
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "short")
    assert Settings.from_env().webhook_secret == expected


def test_bot_token_is_still_required(environment, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN")
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings.from_env()


@pytest.mark.parametrize("id_key,hash_key", [
    ("API_ID", "API_HASH"), ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"), ("TG_API_ID", "TG_API_HASH"),
])
def test_optional_mtproto_credentials_are_read_and_never_shown(environment, monkeypatch, id_key, hash_key):
    monkeypatch.setenv(id_key, "34567")
    monkeypatch.setenv(hash_key, "PRIVATE_API_HASH")
    settings = Settings.from_env()
    assert settings.api_id == 34567 and settings.api_hash == "PRIVATE_API_HASH"
    assert "PRIVATE_API_HASH" not in repr(settings)
    assert "34567" not in repr(settings)
    assert settings.bot_token not in repr(settings)


def test_empty_primary_names_do_not_hide_configured_aliases(environment, monkeypatch):
    monkeypatch.setenv("API_ID", "  ")
    monkeypatch.setenv("API_HASH", "")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "PRIVATE")
    settings = Settings.from_env()
    assert settings.api_id == 123 and settings.api_hash == "PRIVATE"


def test_invalid_api_id_does_not_log_the_value(environment, monkeypatch):
    monkeypatch.setenv("API_ID", "PRIVATE_BAD_ID")
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "PRIVATE_BAD_ID" not in str(error.value)


def test_rejects_timeout_over_idle_limit(environment, monkeypatch):
    monkeypatch.setenv("TRANSLATION_TIMEOUT_SECONDS", "3600")
    with pytest.raises(ValueError, match="TRANSLATION_TIMEOUT_SECONDS"):
        Settings.from_env()


def test_proxy_configuration_and_secret_redaction(environment, monkeypatch):
    monkeypatch.setenv("WEBSITE_PROXIES", "http://user:secret%40word@proxy.example:8080, socks5://[::1]:1080")
    settings = Settings.from_env()
    assert len(settings.website_proxies) == 2
    assert settings.website_proxies[0].playwright_options() == {
        "server": "http://proxy.example:8080", "username": "user", "password": "secret@word"
    }
    assert settings.website_proxies[1].server == "socks5://[::1]:1080"
    monkeypatch.setenv("WEBSITE_PROXIES", "proxy.example:3128")
    assert Settings.from_env().website_proxies[0].server == "http://proxy.example:3128"
    assert "secret" not in repr(settings) and "secret" not in repr(settings.website_proxies)


@pytest.mark.parametrize("value", [
    "vless://secret@example.com:443", "http://secret@example.com", "http://example.com:bad",
    "socks5://user:password@example.com:1080", "http://example.com:8080/path",
    "http://example.com:8080,", ",".join(["http://example.com:8080"] * 11),
])
def test_invalid_proxy_configuration_never_exposes_input(environment, monkeypatch, value):
    monkeypatch.setenv("WEBSITE_PROXIES", value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert value not in str(error.value)


def test_owner_log_channel_and_mongodb_settings(environment, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", "111, 222")
    monkeypatch.setenv("LOG_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://user:secret@cluster.example/db")
    monkeypatch.setenv("MONGODB_DB", "subtranslate_prod")
    settings = Settings.from_env()
    assert settings.owner_ids == frozenset({111, 222})
    assert settings.log_channel_id == -1001234567890
    assert settings.mongodb_database == "subtranslate_prod"
    assert settings.mongodb_storage_limit_mb == 512
    # Credentials must never be printable or logged.
    assert "secret" not in repr(settings)
    assert "cluster.example" not in repr(settings)
    assert settings.mongodb_uri.startswith("mongodb+srv://")

    monkeypatch.setenv("OWNER_ID", "333")  # the singular alias wins
    assert Settings.from_env().owner_ids == frozenset({333})


@pytest.mark.parametrize("value", ["abc", "0", "-5", "12,xx"])
def test_invalid_owner_ids_are_rejected_without_echoing_them(environment, monkeypatch, value):
    monkeypatch.setenv("OWNER_ID", value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert value not in str(error.value)


@pytest.mark.parametrize("value", ["not-a-number", "0"])
def test_invalid_log_channel_is_rejected(environment, monkeypatch, value):
    monkeypatch.setenv("LOG_CHANNEL_ID", value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("value", ["bad/name", "has space", "x" * 65])
def test_invalid_mongodb_database_names_are_rejected(environment, monkeypatch, value):
    monkeypatch.setenv("MONGODB_DB", value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_empty_mongodb_database_uses_the_default(environment, monkeypatch):
    monkeypatch.setenv("MONGODB_DB", "")
    assert Settings.from_env().mongodb_database == "subtranslate"


def test_mongodb_storage_limit_must_be_sane(environment, monkeypatch):
    monkeypatch.setenv("MONGODB_STORAGE_LIMIT_MB", "-1")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("MONGODB_STORAGE_LIMIT_MB", "1024")
    assert Settings.from_env().mongodb_storage_limit_mb == 1024


def test_default_access_mode_follows_the_allowlist(environment, monkeypatch):
    assert Settings.from_env().default_access_mode == "public"
    monkeypatch.setenv("ALLOWED_USER_IDS", "5")
    assert Settings.from_env().default_access_mode == "private"
