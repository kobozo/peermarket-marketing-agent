"""Routing of domain channels to configured Slack report channels."""

from types import SimpleNamespace

from peermarket_agent.slack_routing import report_channel_id


def _settings(**overrides) -> SimpleNamespace:
    defaults = {
        "slack_report_channel_tiktok": "",
        "slack_report_channel_meta": "",
        "slack_report_channel_email": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_maps_each_domain_channel_to_its_configured_id() -> None:
    settings = _settings(
        slack_report_channel_tiktok="C0BJ71Z4YFL",
        slack_report_channel_meta="C0BJ0PUURRR",
        slack_report_channel_email="C0BHRLPM3QX",
    )
    assert report_channel_id(settings, "tiktok") == "C0BJ71Z4YFL"
    assert report_channel_id(settings, "meta") == "C0BJ0PUURRR"
    assert report_channel_id(settings, "email") == "C0BHRLPM3QX"


def test_unset_setting_falls_back_to_none() -> None:
    assert report_channel_id(_settings(), "meta") is None


def test_unknown_channel_falls_back_to_none() -> None:
    settings = _settings(slack_report_channel_meta="C0BJ0PUURRR")
    assert report_channel_id(settings, "seo_pr") is None
    assert report_channel_id(settings, "") is None
