import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Barrier

import pytest

from peermarket_agent.meta_ads import MetaConfig
from peermarket_agent.meta_insights import (
    MetaInsightsError,
    MetaInsightSnapshot,
    fetch_meta_insights,
)

CONFIG = MetaConfig(
    app_id="app-id",
    app_secret="super-secret",
    system_user_token="secret-token",
    ad_account_id="act_123",
    page_id="page-1",
)
START = date(2026, 7, 14)
STOP = date(2026, 7, 16)


class FakeMetaError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int = 100,
        error_type: str = "OAuthException",
        status: int = 400,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self._code = code
        self._error_type = error_type
        self._status = status
        self._transient = transient

    def api_error_code(self):
        return self._code

    def api_error_type(self):
        return self._error_type

    def http_status(self):
        return self._status

    def api_transient_error(self):
        return self._transient


class FakeCursor:
    def __init__(self, pages):
        self._pages = pages

    def __iter__(self):
        for page in self._pages:
            yield from page


@pytest.fixture
def meta_api(monkeypatch):
    class MetaApi:
        pages = [[{}]]
        failures = []
        calls = 0
        requests = []
        initialized_with = []

    class BoundApi:
        def __init__(self, access_token):
            self.access_token = access_token

    api = MetaApi()

    class FakeAd:
        def __init__(self, ad_id, api=None):
            self.ad_id = ad_id
            self.api = api

        def get_insights(self, *, fields, params):
            api.calls += 1
            api.requests.append((self.ad_id, fields, params))
            if api.failures:
                raise api.failures.pop(0)
            return FakeCursor(api.pages)

    monkeypatch.setattr("peermarket_agent.meta_insights.Ad", FakeAd)
    monkeypatch.setattr(
        "peermarket_agent.meta_insights.FacebookAdsApi.init",
        lambda **kwargs: api.initialized_with.append(kwargs) or BoundApi(kwargs["access_token"]),
    )
    monkeypatch.setattr("peermarket_agent.meta_insights.asyncio.sleep", _no_sleep)
    return api


async def _no_sleep(_delay):
    return None


async def test_fetch_normalizes_missing_actions_and_decimal_cents(meta_api):
    meta_api.pages = [[{"spend": "2.17", "impressions": "1062", "actions": []}]]

    snapshot = await fetch_meta_insights(CONFIG, "ad-1", START, STOP)

    assert snapshot.spend_cents == 217
    assert snapshot.impressions == 1062
    assert snapshot.landing_page_views == 0
    assert snapshot.actions == {}


async def test_fetch_sums_paginated_rows_and_action_types(meta_api):
    meta_api.pages = [
        [
            {
                "spend": "1.005",
                "impressions": "100",
                "reach": "80",
                "clicks": "8",
                "inline_link_clicks": "5",
                "outbound_clicks": [{"action_type": "outbound_click", "value": "3"}],
                "actions": [
                    {"action_type": "landing_page_view", "value": "2"},
                    {"action_type": "post_engagement", "value": "4"},
                ],
            }
        ],
        [
            {
                "spend": "1.165",
                "impressions": "25",
                "reach": "20",
                "clicks": "2",
                "inline_link_clicks": "1",
                "outbound_clicks": [{"action_type": "outbound_click", "value": "1"}],
                "actions": [
                    {"action_type": "landing_page_view", "value": "1"},
                    {"action_type": "post_engagement", "value": "2"},
                ],
            }
        ],
    ]

    snapshot = await fetch_meta_insights(CONFIG, "ad-1", START, STOP)

    assert snapshot.spend_cents == 217
    assert snapshot.impressions == 125
    assert snapshot.reach == 100
    assert snapshot.clicks == 10
    assert snapshot.inline_link_clicks == 6
    assert snapshot.outbound_clicks == 4
    assert snapshot.landing_page_views == 3
    assert snapshot.actions == {"landing_page_view": 3, "post_engagement": 6}
    assert snapshot.ctr == Decimal("8")
    assert snapshot.cpc_cents == 22
    assert snapshot.cpm_cents == 1736
    assert snapshot.frequency == Decimal("1.25")


async def test_fetch_uses_exact_fields_window_and_utc_retrieval_time(meta_api):
    before = datetime.now(UTC)

    snapshot = await fetch_meta_insights(CONFIG, "ad-1", START, STOP)

    after = datetime.now(UTC)
    ad_id, fields, params = meta_api.requests[0]
    assert ad_id == "ad-1"
    assert fields == [
        "spend",
        "impressions",
        "reach",
        "clicks",
        "inline_link_clicks",
        "outbound_clicks",
        "actions",
        "ctr",
        "cpc",
        "cpm",
        "frequency",
    ]
    assert params == {
        "time_range": {"since": "2026-07-14", "until": "2026-07-16"},
        "time_increment": 1,
    }
    assert snapshot.window_start == START
    assert snapshot.window_stop == STOP
    assert before <= snapshot.retrieved_at <= after
    assert snapshot.retrieved_at.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        snapshot.ad_id = "changed"


def test_snapshot_actions_are_deeply_immutable_and_defensively_copied():
    source_actions = {"landing_page_view": 3}
    snapshot = MetaInsightSnapshot(
        ad_id="ad-1",
        window_start=START,
        window_stop=STOP,
        retrieved_at=datetime(2026, 7, 16, tzinfo=UTC),
        spend_cents=217,
        impressions=100,
        reach=80,
        clicks=8,
        inline_link_clicks=5,
        outbound_clicks=3,
        landing_page_views=3,
        ctr=Decimal("8"),
        cpc_cents=27,
        cpm_cents=2170,
        frequency=Decimal("1.25"),
        actions=source_actions,
    )

    source_actions["landing_page_view"] = 99

    assert snapshot.actions == {"landing_page_view": 3}
    with pytest.raises(TypeError):
        snapshot.actions["landing_page_view"] = 4


async def test_concurrent_fetches_bind_each_ad_to_its_own_initialized_api(monkeypatch):
    barrier = Barrier(2)
    global_default = {"api": None}

    class BoundApi:
        def __init__(self, token):
            self.token = token

    def fake_init(**kwargs):
        api = BoundApi(kwargs["access_token"])
        global_default["api"] = api
        barrier.wait(timeout=2)
        return api

    class FakeAd:
        def __init__(self, ad_id, api=None):
            self.ad_id = ad_id
            self.api = api if api is not None else global_default["api"]

        def get_insights(self, *, fields, params):
            del fields, params
            return [{"actions": [{"action_type": self.api.token, "value": "1"}]}]

    monkeypatch.setattr("peermarket_agent.meta_insights.FacebookAdsApi.init", fake_init)
    monkeypatch.setattr("peermarket_agent.meta_insights.Ad", FakeAd)
    first = MetaConfig("app", "secret", "token-ad-1", "act_1", "page")
    second = MetaConfig("app", "secret", "token-ad-2", "act_1", "page")

    first_snapshot, second_snapshot = await asyncio.gather(
        fetch_meta_insights(first, "ad-1", START, STOP),
        fetch_meta_insights(second, "ad-2", START, STOP),
    )

    assert first_snapshot.actions == {"token-ad-1": 1}
    assert second_snapshot.actions == {"token-ad-2": 1}


async def test_transient_failure_retries_at_most_three_attempts(meta_api):
    meta_api.failures = [
        FakeMetaError("temporary", transient=True),
        FakeMetaError("temporary", transient=True),
    ]

    await fetch_meta_insights(CONFIG, "ad-1", START, STOP, max_attempts=3)

    assert meta_api.calls == 3


async def test_rate_limit_retries_and_stops_at_attempt_bound(meta_api):
    meta_api.failures = [
        FakeMetaError("rate limited", code=17, status=429),
        FakeMetaError("rate limited", code=17, status=429),
        FakeMetaError("rate limited", code=17, status=429),
    ]

    with pytest.raises(MetaInsightsError) as caught:
        await fetch_meta_insights(CONFIG, "ad-1", START, STOP, max_attempts=3)

    assert meta_api.calls == 3
    assert caught.value.transient is True


async def test_permanent_failure_is_not_retried_and_credentials_are_redacted(meta_api):
    meta_api.failures = [
        FakeMetaError(
            "bad token secret-token and super-secret",
            code=190,
            error_type="OAuthException secret-token super-secret",
            status=400,
        )
    ]

    with pytest.raises(MetaInsightsError) as caught:
        await fetch_meta_insights(CONFIG, "ad-1", START, STOP)

    message = str(caught.value)
    assert meta_api.calls == 1
    assert caught.value.transient is False
    assert "secret-token" not in message
    assert "super-secret" not in message
    assert "bad token" not in message
    assert "OAuthException" not in message
    assert message == "Meta Insights request failed (category=authentication, code=190)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_empty_denominators_produce_no_ratio_metrics(meta_api):
    meta_api.pages = [[{"spend": "0", "impressions": "0", "reach": "0", "clicks": "0"}]]

    snapshot = await fetch_meta_insights(CONFIG, "ad-1", START, STOP)

    assert snapshot.ctr is None
    assert snapshot.cpc_cents is None
    assert snapshot.cpm_cents is None
    assert snapshot.frequency is None


async def test_invalid_attempt_bound_does_not_touch_sdk(meta_api):
    with pytest.raises(ValueError, match="max_attempts must be between 1 and 3"):
        await fetch_meta_insights(CONFIG, "ad-1", START, STOP, max_attempts=4)

    assert meta_api.calls == 0
