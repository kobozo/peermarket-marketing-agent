"""Stable, attributable PeerMarket campaign destinations."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def build_campaign_url(base_url: str, draft_id: int) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "peermarket.eu",
        "www.peermarket.eu",
    }:
        raise ValueError("campaign destination must be HTTPS PeerMarket")
    campaign_query = [
        ("utm_source", "facebook"),
        ("utm_medium", "paid_social"),
        ("utm_campaign", "peermarket"),
        ("utm_content", f"draft-{draft_id}"),
    ]
    campaign_keys = {key for key, _ in campaign_query}
    query = [
        pair
        for pair in parse_qsl(parsed.query, keep_blank_values=True)
        if pair[0] not in campaign_keys
    ]
    query.extend(campaign_query)
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))
