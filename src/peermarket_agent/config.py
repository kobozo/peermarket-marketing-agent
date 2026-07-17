"""Env-driven configuration for the marketing agent."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
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

    # Uploaded video processing
    video_media_root: Path = Path("data/video-media")
    video_max_file_bytes: int = 209715200
    video_max_clips: int = 8
    video_max_duration_seconds: int = 60
    video_retention_days: int = 30

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
    meta_insights_lookback_days: int = Field(default=3, ge=1, le=30)
    meta_account_timezone: str = "Europe/Brussels"
    meta_no_delivery_grace_hours: int = Field(default=2, ge=0, le=168)
    performance_snapshot_max_age_hours: int = Field(default=2, ge=1, le=168)
    learning_min_impressions: int = Field(default=1000, ge=1, le=10_000_000)
    learning_min_landing_page_views: int = Field(default=30, ge=1, le=1_000_000)
    learning_min_registrations: int = Field(default=10, ge=1, le=1_000_000)

    # Autonomous Meta lifecycle (disabled, read-only defaults)
    meta_autonomy_enabled: bool = False
    meta_autonomy_shadow: bool = True
    meta_autonomy_campaign_ids_csv: str = ""
    meta_autonomy_max_replacements_24h: int = Field(default=1, ge=0, le=10)
    meta_autonomy_cooldown_hours: int = Field(default=24, ge=1, le=168)
    meta_autonomy_max_test_days: int = Field(default=7, ge=1, le=30)
    meta_autonomy_max_increase_percent: int = Field(default=20, ge=0, le=20)
    meta_autonomy_max_daily_budget_eur: int = Field(default=20, ge=5, le=20)

    @field_validator("meta_autonomy_campaign_ids_csv")
    @classmethod
    def _validate_autonomy_campaign_ids(cls, value: str) -> str:
        campaign_ids = tuple(item.strip() for item in value.split(",") if item.strip())
        if any(
            not campaign_id.isascii() or not campaign_id.isdecimal() for campaign_id in campaign_ids
        ):
            raise ValueError("autonomy campaign allowlist IDs must be numeric")
        return value

    @property
    def meta_autonomy_campaign_ids(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.meta_autonomy_campaign_ids_csv.split(",") if item.strip()
        )

    @model_validator(mode="after")
    def _require_allowlist_for_live_autonomy(self) -> "Settings":
        if self.meta_autonomy_enabled and not self.meta_autonomy_shadow:
            if not self.meta_autonomy_campaign_ids:
                raise ValueError("live autonomy requires a non-empty campaign allowlist")
        return self

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
