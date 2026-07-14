"""Meta Ads connector tests — no real API calls."""

from unittest.mock import MagicMock

import pytest

from peermarket_agent.meta_ads import (
    MetaActivationResult,
    MetaAdsDisabled,
    MetaAdsError,
    MetaConfig,
    activate_meta_ad,
    create_meta_ad_paused,
)

_FULL_CONFIG = MetaConfig(
    app_id="111",
    app_secret="s",
    system_user_token="super-secret-token",
    ad_account_id="act_999",
)


def _patch_meta_sdk(
    monkeypatch,
    *,
    campaign_id="c1",
    adset_id="as1",
    image_hash="ih1",
    creative_id="cr1",
    ad_id="ad1",
    raise_on=None,
):
    """Patch facebook_business AdAccount + FacebookAdsApi to return canned IDs."""
    monkeypatch.setattr(
        "peermarket_agent.meta_ads.FacebookAdsApi.init",
        lambda *a, **kw: None,
    )
    fake_account = MagicMock()

    def maybe_raise(method_name):
        if raise_on == method_name:
            from facebook_business.exceptions import FacebookRequestError

            raise FacebookRequestError(
                "Test",
                request_context={},
                http_status=400,
                http_headers={},
                body={"error": {"message": "boom"}},
            )

    def make_campaign(**kwargs):
        maybe_raise("campaign")
        return {"id": campaign_id}

    def make_adset(**kwargs):
        maybe_raise("adset")
        return {"id": adset_id}

    def make_image(**kwargs):
        maybe_raise("image")
        return {"hash": image_hash}

    def make_creative(**kwargs):
        maybe_raise("creative")
        return {"id": creative_id}

    def make_ad(**kwargs):
        maybe_raise("ad")
        return {"id": ad_id}

    fake_account.create_campaign = MagicMock(side_effect=lambda **kw: make_campaign(**kw))
    fake_account.create_ad_set = MagicMock(side_effect=lambda **kw: make_adset(**kw))
    fake_account.create_ad_image = MagicMock(side_effect=lambda **kw: make_image(**kw))
    fake_account.create_ad_creative = MagicMock(side_effect=lambda **kw: make_creative(**kw))
    fake_account.create_ad = MagicMock(side_effect=lambda **kw: make_ad(**kw))

    monkeypatch.setattr(
        "peermarket_agent.meta_ads.AdAccount",
        lambda *a, **kw: fake_account,
    )
    return fake_account


async def test_create_paused_ad_returns_result_with_ids(monkeypatch):
    _patch_meta_sdk(monkeypatch)
    result = await create_meta_ad_paused(
        config=_FULL_CONFIG,
        name="test-ad",
        primary_text="x" * 150,
        headline="Hello",
        description="World",
        cta_type="LEARN_MORE",
        landing_page_url="https://peermarket.eu/?utm_source=meta",
        image_bytes=b"PNG_DATA",
        audience_profile_key="declutterers",
        daily_budget_eur=10,
    )
    assert result.ad_id == "ad1"
    assert result.campaign_id == "c1"
    assert result.ad_set_id == "as1"
    assert result.creative_id == "cr1"
    assert result.status == "PAUSED"
    assert "selected_ad_ids=ad1" in result.ads_manager_url
    assert "act=999" in result.ads_manager_url


async def test_create_paused_ad_disabled_when_credentials_missing():
    config = MetaConfig(app_id="", app_secret="", system_user_token="", ad_account_id="")
    with pytest.raises(MetaAdsDisabled, match="missing credentials"):
        await create_meta_ad_paused(
            config=config,
            name="x",
            primary_text="x" * 150,
            headline="x",
            description="x",
            cta_type="LEARN_MORE",
            landing_page_url="https://x",
            image_bytes=None,
            audience_profile_key="declutterers",
            daily_budget_eur=5,
        )


async def test_create_paused_ad_rejects_invalid_cta(monkeypatch):
    _patch_meta_sdk(monkeypatch)
    with pytest.raises(MetaAdsError, match="cta_type"):
        await create_meta_ad_paused(
            config=_FULL_CONFIG,
            name="x",
            primary_text="x" * 150,
            headline="x",
            description="x",
            cta_type="CLICK_HERE",
            landing_page_url="https://x",
            image_bytes=None,
            audience_profile_key="declutterers",
            daily_budget_eur=5,
        )


async def test_create_paused_ad_rejects_unknown_audience(monkeypatch):
    _patch_meta_sdk(monkeypatch)
    with pytest.raises(MetaAdsError, match="audience profile"):
        await create_meta_ad_paused(
            config=_FULL_CONFIG,
            name="x",
            primary_text="x" * 150,
            headline="x",
            description="x",
            cta_type="LEARN_MORE",
            landing_page_url="https://x",
            image_bytes=None,
            audience_profile_key="not-real",
            daily_budget_eur=5,
        )


async def test_create_paused_ad_skips_image_upload_when_none(monkeypatch):
    fake = _patch_meta_sdk(monkeypatch)
    await create_meta_ad_paused(
        config=_FULL_CONFIG,
        name="x",
        primary_text="x" * 150,
        headline="x",
        description="x",
        cta_type="LEARN_MORE",
        landing_page_url="https://x",
        image_bytes=None,
        audience_profile_key="declutterers",
        daily_budget_eur=5,
    )
    fake.create_ad_image.assert_not_called()


async def test_create_paused_ad_uploads_image_when_provided(monkeypatch):
    fake = _patch_meta_sdk(monkeypatch)
    await create_meta_ad_paused(
        config=_FULL_CONFIG,
        name="x",
        primary_text="x" * 150,
        headline="x",
        description="x",
        cta_type="LEARN_MORE",
        landing_page_url="https://x",
        image_bytes=b"PNG",
        audience_profile_key="declutterers",
        daily_budget_eur=5,
    )
    fake.create_ad_image.assert_called_once()


async def test_create_paused_ad_passes_daily_budget_in_cents(monkeypatch):
    fake = _patch_meta_sdk(monkeypatch)
    await create_meta_ad_paused(
        config=_FULL_CONFIG,
        name="x",
        primary_text="x" * 150,
        headline="x",
        description="x",
        cta_type="LEARN_MORE",
        landing_page_url="https://x",
        image_bytes=None,
        audience_profile_key="declutterers",
        daily_budget_eur=10,
    )
    call_kwargs = fake.create_ad_set.call_args.kwargs["params"]
    # AdSet.Field.daily_budget maps to "daily_budget"
    assert call_kwargs.get("daily_budget") == 1000


async def test_create_paused_ad_raises_meta_ads_error_on_api_failure(monkeypatch):
    _patch_meta_sdk(monkeypatch, raise_on="campaign")
    with pytest.raises(MetaAdsError, match="Meta API error"):
        await create_meta_ad_paused(
            config=_FULL_CONFIG,
            name="x",
            primary_text="x" * 150,
            headline="x",
            description="x",
            cta_type="LEARN_MORE",
            landing_page_url="https://x",
            image_bytes=None,
            audience_profile_key="declutterers",
            daily_budget_eur=5,
        )


async def test_create_paused_ad_all_resources_status_paused(monkeypatch):
    fake = _patch_meta_sdk(monkeypatch)
    await create_meta_ad_paused(
        config=_FULL_CONFIG,
        name="x",
        primary_text="x" * 150,
        headline="x",
        description="x",
        cta_type="LEARN_MORE",
        landing_page_url="https://x",
        image_bytes=None,
        audience_profile_key="declutterers",
        daily_budget_eur=5,
    )
    # Campaign + AdSet + Ad all created with status PAUSED
    campaign_params = fake.create_campaign.call_args.kwargs["params"]
    adset_params = fake.create_ad_set.call_args.kwargs["params"]
    ad_params = fake.create_ad.call_args.kwargs["params"]
    assert "PAUSED" in str(campaign_params).upper()
    assert "PAUSED" in str(adset_params).upper()
    assert "PAUSED" in str(ad_params).upper()


def _patch_resources(
    monkeypatch,
    statuses,
    *,
    fail_on=None,
    rollback_fail_on=None,
    rollback_error_message=None,
):
    calls = []

    class FakeResource:
        def __init__(self, resource_type, resource_id):
            self.resource_type = resource_type
            self.resource_id = resource_id

        def api_update(self, *, params):
            status = params["status"]
            calls.append(("update", self.resource_type, self.resource_id, status))
            if status == "ACTIVE" and fail_on == self.resource_type:
                raise RuntimeError(f"failed to activate {self.resource_type}")
            if status == "PAUSED" and rollback_fail_on == self.resource_type:
                raise RuntimeError(
                    rollback_error_message or f"failed to pause {self.resource_type}"
                )

        def api_get(self, *, fields):
            calls.append(("get", self.resource_type, self.resource_id, tuple(fields)))
            return statuses[self.resource_type]

    monkeypatch.setattr(
        "peermarket_agent.meta_ads.Campaign",
        lambda resource_id: FakeResource("campaign", resource_id),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_ads.AdSet",
        lambda resource_id: FakeResource("ad_set", resource_id),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_ads.Ad",
        lambda resource_id: FakeResource("ad", resource_id),
    )
    monkeypatch.setattr("peermarket_agent.meta_ads._init_api", lambda config: None)
    return calls


@pytest.mark.parametrize("ad_effective_status", ["ACTIVE", "IN_PROCESS", "PENDING_REVIEW"])
async def test_activate_meta_ad_orders_updates_and_accepts_review_states(
    monkeypatch, ad_effective_status
):
    statuses = {
        "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad": {"status": "ACTIVE", "effective_status": ad_effective_status},
    }
    calls = _patch_resources(monkeypatch, statuses)

    result = await activate_meta_ad(
        _FULL_CONFIG,
        {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"},
    )

    assert isinstance(result, MetaActivationResult)
    assert result.campaign == statuses["campaign"]
    assert result.ad_set == statuses["ad_set"]
    assert result.ad == statuses["ad"]
    assert [call[1] for call in calls if call[0] == "update"] == [
        "campaign",
        "ad_set",
        "ad",
    ]
    assert [call[1] for call in calls if call[0] == "get"] == [
        "campaign",
        "ad_set",
        "ad",
    ]


async def test_activation_failure_rolls_back_child_to_parent_and_reports_context(monkeypatch):
    statuses = {
        "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad_set": {"status": "PAUSED", "effective_status": "PAUSED"},
        "ad": {"status": "PAUSED", "effective_status": "PAUSED"},
    }
    calls = _patch_resources(
        monkeypatch,
        statuses,
        fail_on="ad_set",
        rollback_fail_on="ad",
    )
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(_FULL_CONFIG, ids)

    error = caught.value
    assert error.phase == "activate_ad_set"
    assert error.resource_ids == ids
    assert error.observed_statuses == statuses
    assert error.rollback_errors == {"ad": "failed to pause ad"}
    assert [call[1] for call in calls if call[0] == "update" and call[3] == "PAUSED"] == [
        "ad",
        "ad_set",
        "campaign",
    ]
    rendered = str(error)
    assert "activate_ad_set" in rendered
    assert "system_user_token" not in rendered
    assert _FULL_CONFIG.system_user_token not in rendered


async def test_activation_error_redacts_credentials_from_rollback_errors(monkeypatch):
    statuses = {
        "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad_set": {"status": "PAUSED", "effective_status": "PAUSED"},
        "ad": {"status": "PAUSED", "effective_status": "PAUSED"},
    }
    _patch_resources(
        monkeypatch,
        statuses,
        fail_on="ad_set",
        rollback_fail_on="ad",
        rollback_error_message=f"request included {_FULL_CONFIG.system_user_token}",
    )

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(
            _FULL_CONFIG,
            {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"},
        )

    assert caught.value.rollback_errors == {"ad": "request included [REDACTED]"}
    assert _FULL_CONFIG.system_user_token not in str(caught.value)
