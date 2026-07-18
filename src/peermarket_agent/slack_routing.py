"""Map domain channels to configured Slack report channels."""

_CHANNEL_SETTING_FIELDS = {
    "tiktok": "slack_report_channel_tiktok",
    "meta": "slack_report_channel_meta",
    "email": "slack_report_channel_email",
}


def report_channel_id(settings, channel: str) -> str | None:
    """Slack channel ID for a domain channel; None means fall back to the founder DM."""
    field = _CHANNEL_SETTING_FIELDS.get(channel)
    if field is None:
        return None
    return getattr(settings, field, "") or None
