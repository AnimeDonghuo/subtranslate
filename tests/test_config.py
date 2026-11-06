import pytest

from subtranslate.config import Settings


@pytest.fixture
def environment(monkeypatch):
    for key in ("BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "WEBHOOK_BASE_URL", "KOYEB_PUBLIC_DOMAIN", "PORT", "ALLOWED_USER_IDS", "TRANSLATION_TIMEOUT_SECONDS", "CHROMIUM_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:example")
    monkeypatch.setenv("KOYEB_PUBLIC_DOMAIN", "demo-example.koyeb.app")


def test_koyeb_domain_and_allowlist(environment, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "123, 456")
    settings = Settings.from_env()
    assert settings.webhook_url == "https://demo-example.koyeb.app/telegram/webhook"
    assert settings.allowed_user_ids == frozenset({123, 456})
    assert settings.port == 8080


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


def test_rejects_timeout_over_idle_limit(environment, monkeypatch):
    monkeypatch.setenv("TRANSLATION_TIMEOUT_SECONDS", "3600")
    with pytest.raises(ValueError, match="TRANSLATION_TIMEOUT_SECONDS"):
        Settings.from_env()
