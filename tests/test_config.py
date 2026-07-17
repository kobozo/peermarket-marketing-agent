"""Tests for env-driven configuration."""

import pytest
from pydantic import ValidationError

from peermarket_agent.config import Settings, get_settings


@pytest.fixture
def settings_payload():
    return {
        "anthropic_api_key": "sk-ant-test",
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "agent_db_url": "postgresql+asyncpg://x:y@localhost/z",
        "peermarket_prod_db_readonly_url": "postgresql+asyncpg://r:o@host/peer",
        "github_app_id": 1,
        "github_app_private_key": (
            "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"
        ),
        "github_app_installation_id": 1,
        "recraft_api_key": "rk",
        "resend_api_key": "re",
        "backblaze_b2_key_id": "kid",
        "backblaze_b2_app_key": "akey",
        "backblaze_b2_bucket": "bucket",
        "backblaze_b2_endpoint": "endpoint",
    }


def test_autonomy_defaults_are_safe(settings_payload):
    settings = Settings(**settings_payload)
    assert settings.meta_autonomy_enabled is False
    assert settings.meta_autonomy_shadow is True
    assert settings.meta_autonomy_campaign_ids == ()
    assert settings.meta_autonomy_max_replacements_24h == 1
    assert settings.meta_autonomy_cooldown_hours == 24
    assert settings.meta_autonomy_max_test_days == 7
    assert settings.meta_autonomy_max_daily_budget_eur == 20
    assert settings.meta_autonomy_max_increase_percent == 20
    assert settings.meta_autonomy_experiment_id == ""
    assert settings.meta_autonomy_variant_count == 3


@pytest.mark.parametrize("count", [0, 1, 2, 4, 99])
def test_hook_experiment_variant_count_is_exactly_three(settings_payload, count):
    with pytest.raises(ValidationError, match="3"):
        Settings(**settings_payload, meta_autonomy_variant_count=count)


def test_configured_first_hook_experiment_requires_exact_campaign_allowlist(settings_payload):
    with pytest.raises(ValidationError, match="120249125021520342"):
        Settings(
            **settings_payload,
            meta_autonomy_experiment_id="draft-156-hooks-v1",
            meta_autonomy_campaign_ids_csv="999",
        )
    configured = Settings(
        **settings_payload,
        meta_autonomy_experiment_id="draft-156-hooks-v1",
        meta_autonomy_campaign_ids_csv="120249125021520342",
    )
    assert configured.meta_autonomy_experiment_id == "draft-156-hooks-v1"


def test_autonomy_campaign_allowlist_is_trimmed(settings_payload):
    settings = Settings(
        **settings_payload,
        meta_autonomy_campaign_ids_csv=" 123,456,  ,789 ",
    )
    assert settings.meta_autonomy_campaign_ids == ("123", "456", "789")


@pytest.mark.parametrize("campaign_ids", ["abc", "123,act_456", "123, 45x", "١٢٣"])
def test_autonomy_campaign_allowlist_rejects_invalid_ids(settings_payload, campaign_ids):
    with pytest.raises(ValidationError):
        Settings(**settings_payload, meta_autonomy_campaign_ids_csv=campaign_ids)


def test_execution_requires_a_nonempty_allowlist(settings_payload):
    with pytest.raises(ValidationError, match="allowlist"):
        Settings(
            **settings_payload,
            meta_autonomy_enabled=True,
            meta_autonomy_shadow=False,
        )


def test_shadow_mode_can_run_without_an_allowlist(settings_payload):
    settings = Settings(
        **settings_payload,
        meta_autonomy_enabled=True,
        meta_autonomy_shadow=True,
    )
    assert settings.meta_autonomy_campaign_ids == ()


def test_settings_required_fields_missing_raises(monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "AGENT_DB_URL",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
        "PEERMARKET_PROD_DB_READONLY_URL",
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
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
    )
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("PEERMARKET_PROD_DB_READONLY_URL", "postgresql+asyncpg://r:o@host/peer")
    monkeypatch.setenv("RECRAFT_API_KEY", "rk-test")
    monkeypatch.setenv("RESEND_API_KEY", "re-test")
    monkeypatch.setenv("BACKBLAZE_B2_KEY_ID", "kid")
    monkeypatch.setenv("BACKBLAZE_B2_APP_KEY", "akey")
    monkeypatch.setenv("BACKBLAZE_B2_BUCKET", "peermarket-agent-backups")
    monkeypatch.setenv("BACKBLAZE_B2_ENDPOINT", "s3.eu-central-003.backblazeb2.com")
    monkeypatch.setenv("META_PAGE_ID", "61592144690879")
    get_settings.cache_clear()
    s = get_settings()
    assert s.anthropic_api_key == "sk-ant-test"
    assert s.slack_bot_token == "xoxb-test"
    assert s.github_app_id == 12345
    assert s.github_app_installation_id == 67890
    assert s.meta_page_id == "61592144690879"
    assert s.timezone == "Europe/Brussels"
    assert s.meta_account_timezone == "Europe/Brussels"
    assert s.performance_snapshot_max_age_hours == 2


def test_meta_auto_activate_defaults_to_false(monkeypatch):
    monkeypatch.delenv("META_AUTO_ACTIVATE", raising=False)

    assert Settings.model_fields["meta_auto_activate"].default is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_meta_auto_activate_parses_boolean_env(monkeypatch, value, expected):
    monkeypatch.setenv("META_AUTO_ACTIVATE", value)

    assert (
        Settings.model_validate(
            {
                "anthropic_api_key": "sk-ant-test",
                "slack_bot_token": "xoxb-test",
                "slack_app_token": "xapp-test",
                "agent_db_url": "postgresql+asyncpg://x:y@localhost/z",
                "peermarket_prod_db_readonly_url": "postgresql+asyncpg://r:o@host/peer",
                "github_app_id": 1,
                "github_app_private_key": (
                    "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"
                ),
                "github_app_installation_id": 1,
                "recraft_api_key": "rk",
                "resend_api_key": "re",
                "backblaze_b2_key_id": "kid",
                "backblaze_b2_app_key": "akey",
                "backblaze_b2_bucket": "bucket",
                "backblaze_b2_endpoint": "endpoint",
            }
        ).meta_auto_activate
        is expected
    )


def test_settings_cached(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("AGENT_DB_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
    )
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


def test_settings_loads_pem_from_file(monkeypatch, tmp_path):
    pem_path = tmp_path / "github-app.pem"
    pem_body = "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----"
    pem_path.write_text(pem_body)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("AGENT_DB_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", str(pem_path))
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
    assert s.github_app_private_key == pem_body
