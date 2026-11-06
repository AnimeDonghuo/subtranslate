import pytest

from subtranslate.config import Settings


@pytest.fixture
def environment(monkeypatch):
    for key in ("BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "WEBHOOK_BASE_URL", "KOYEB_PUBLIC_DOMAIN", "PORT", "ALLOWED_USER_IDS", "TRANSLATION_TIMEOUT_SECONDS", "CHROMIUM_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:example")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "a_long_random_secret_1234")
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


def test_rejects_weak_or_invalid_secret(environment, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "short")
    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings.from_env()


def test_rejects_timeout_over_idle_limit(environment, monkeypatch):
    monkeypatch.setenv("TRANSLATION_TIMEOUT_SECONDS", "3600")
    with pytest.raises(ValueError, match="TRANSLATION_TIMEOUT_SECONDS"):
        Settings.from_env()
