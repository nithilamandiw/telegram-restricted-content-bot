import pytest

from telegram_downloader.config import Settings


def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1234")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("BOT_TOKEN", "1234:" + "a" * 35)
    monkeypatch.setenv("ALLOWED_USER_IDS", "101,202")
    monkeypatch.setattr("telegram_downloader.config.load_dotenv", lambda: None)


def test_valid_config_and_defaults(monkeypatch):
    env(monkeypatch)
    settings = Settings.load()
    assert settings.max_file_bytes == 2_000_000_000
    assert settings.allowed_user_ids == frozenset({101, 202})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ALLOWED_USER_IDS", ""),
        ("ALLOWED_USER_IDS", "-1"),
        ("ALLOWED_USER_IDS", "username"),
        ("TELEGRAM_API_ID", "0"),
        ("TELEGRAM_API_HASH", "placeholder"),
        ("MAX_FILE_BYTES", "2000000001"),
        ("MAX_PENDING_PER_USER", "0"),
        ("BOT_API_BASE_URL", "https://api.telegram.org"),
        ("BOT_API_BASE_URL", "http://localhost:8081/bot/token"),
    ],
)
def test_unsafe_or_invalid_config_is_rejected(monkeypatch, key, value):
    env(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.load()


def test_login_does_not_require_bot_credentials(monkeypatch):
    env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN")
    monkeypatch.delenv("ALLOWED_USER_IDS")
    assert Settings.load(require_bot=False).api_id == 1234
