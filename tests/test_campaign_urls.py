import pytest

from peermarket_agent.campaign_urls import build_campaign_url


def test_build_campaign_url_preserves_existing_query():
    assert build_campaign_url("https://peermarket.eu/?lang=nl", 200) == (
        "https://peermarket.eu/?lang=nl&utm_source=facebook&utm_medium=paid_social"
        "&utm_campaign=peermarket&utm_content=draft-200"
    )


@pytest.mark.parametrize("url", ["http://peermarket.eu/", "https://example.com/"])
def test_build_campaign_url_rejects_unsafe_destination(url):
    with pytest.raises(ValueError):
        build_campaign_url(url, 200)
