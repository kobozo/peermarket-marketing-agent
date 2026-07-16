"""Env-driven configuration for the marketing agent."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    slack_founder_user_id: str = ""  # DM target for credit-low + other founder alerts

    # Database
    agent_db_url: str  # local Postgres on VM 129
    peermarket_prod_db_readonly_url: str

    # GitHub App
    github_app_id: int
    # Accepts either a literal PEM (starting with -----BEGIN) or a path to a
    # file containing the PEM. systemd's EnvironmentFile= cannot carry
    # multi-line values, so deploys write the PEM to disk and pass the path.
    github_app_private_key: str
    github_app_installation_id: int

    @field_validator("github_app_private_key")
    @classmethod
    def _resolve_pem(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("-----BEGIN"):
            return v
        path = Path(v)
        if not path.exists():
            raise ValueError(
                f"github_app_private_key is neither a PEM nor an existing file path: {v!r}"
            )
        return path.read_text()

    # Recraft (image gen)
    recraft_api_key: str

    # Gemini / Nano Banana (image editing — empty disables it gracefully)
    gemini_api_key: str = ""

    # Meta Marketing API (creates PAUSED ads — empty disables the connector)
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_system_user_token: str = ""
    meta_ad_account_id: str = ""  # 'act_<numeric>'
    meta_auto_activate: bool = False
    meta_page_id: str = ""
    meta_insights_enabled: bool = False
    peermarket_attribution_enabled: bool = False

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
