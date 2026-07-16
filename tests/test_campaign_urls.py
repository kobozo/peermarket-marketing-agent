import pytest

from peermarket_agent.campaign_urls import build_campaign_url


def test_build_campaign_url_preserves_existing_query():
    assert build_campaign_url("https://peermarket.eu/?lang=nl", 200) == (
        "https://peermarket.eu/?lang=nl&utm_source=facebook&utm_medium=paid_social"
        "&utm_campaign=peermarket&utm_content=draft-200"
    )


def test_build_campaign_url_preserves_ordered_duplicate_query_pairs_and_fragment():
    assert build_campaign_url(
        "https://peermarket.eu/market?tag=chair&lang=nl&tag=table#results", 201
    ) == (
        "https://peermarket.eu/market?tag=chair&lang=nl&tag=table"
        "&utm_source=facebook&utm_medium=paid_social&utm_campaign=peermarket"
        "&utm_content=draft-201#results"
    )


def test_build_campaign_url_replaces_stale_utm_fields_without_duplicates():
    result = build_campaign_url(
        "https://www.peermarket.eu/?utm_source=old&lang=fr&utm_source=older"
        "&utm_medium=legacy&utm_campaign=launch&utm_content=old-ad",
        202,
    )

    assert result == (
        "https://www.peermarket.eu/?lang=fr&utm_source=facebook"
        "&utm_medium=paid_social&utm_campaign=peermarket&utm_content=draft-202"
    )
    assert result.count("utm_source=") == 1


@pytest.mark.parametrize("url", ["http://peermarket.eu/", "https://example.com/"])
def test_build_campaign_url_rejects_unsafe_destination(url):
    with pytest.raises(ValueError):
        build_campaign_url(url, 200)
