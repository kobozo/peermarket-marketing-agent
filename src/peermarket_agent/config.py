"""Env-driven configuration for the marketing agent."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: str
    claude_sonnet_model: str = "claude-sonnet-4-6"
    claude_opus_model: str = "claude-opus-4-7"

    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str = ""

    # Database
    agent_db_url: str  # local Postgres on VM 129
    peermarket_prod_db_readonly_url: str

    # GitHub App
    github_app_id: int
    github_app_private_key: str
    github_app_installation_id: int

    # Recraft (image gen)
    recraft_api_key: str

    # Resend (email)
    resend_api_key: str

    # Backblaze B2 (backups)
    backblaze_b2_key_id: str
    backblaze_b2_app_key: str
    backblaze_b2_bucket: str
    backblaze_b2_endpoint: str

    # Operational
    timezone: str = "Europe/Brussels"
    log_level: str = "INFO"
    healthcheck_port: int = 8090
    enable_smoke_on_boot: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
