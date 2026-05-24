"""Tests for env-driven configuration."""
import pytest
from pydantic import ValidationError

from peermarket_agent.config import Settings, get_settings


def test_settings_required_fields_missing_raises(monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
        "AGENT_DB_URL", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID", "PEERMARKET_PROD_DB_READONLY_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()


def test_settings_loaded_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig-test")
    monkeypatch.setenv("AGENT_DB_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("PEERMARKET_PROD_DB_READONLY_URL", "postgresql+asyncpg://r:o@host/peer")
    monkeypatch.setenv("RECRAFT_API_KEY", "rk-test")
    monkeypatch.setenv("RESEND_API_KEY", "re-test")
    monkeypatch.setenv("BACKBLAZE_B2_KEY_ID", "kid")
    monkeypatch.setenv("BACKBLAZE_B2_APP_KEY", "akey")
    monkeypatch.setenv("BACKBLAZE_B2_BUCKET", "peermarket-agent-backups")
    monkeypatch.setenv("BACKBLAZE_B2_ENDPOINT", "s3.eu-central-003.backblazeb2.com")
    get_settings.cache_clear()
    s = get_settings()
    assert s.anthropic_api_key == "sk-ant-test"
    assert s.slack_bot_token == "xoxb-test"
    assert s.github_app_id == 12345
    assert s.github_app_installation_id == 67890
    assert s.timezone == "Europe/Brussels"


def test_settings_cached(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("AGENT_DB_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("PEERMARKET_PROD_DB_READONLY_URL", "postgresql+asyncpg://r:o@host/peer")
    monkeypatch.setenv("RECRAFT_API_KEY", "rk-test")
    monkeypatch.setenv("RESEND_API_KEY", "re-test")
    monkeypatch.setenv("BACKBLAZE_B2_KEY_ID", "kid")
    monkeypatch.setenv("BACKBLAZE_B2_APP_KEY", "akey")
    monkeypatch.setenv("BACKBLAZE_B2_BUCKET", "peermarket-agent-backups")
    monkeypatch.setenv("BACKBLAZE_B2_ENDPOINT", "s3.eu-central-003.backblazeb2.com")
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
