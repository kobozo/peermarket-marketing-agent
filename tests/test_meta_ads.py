"""Meta Ads connector tests — no real API calls."""

import traceback
from unittest.mock import AsyncMock, MagicMock

import pytest

from peermarket_agent.meta_ads import (
    MetaActivationResult,
    MetaAdsDisabled,
    MetaAdsError,
    MetaBundleLocale,
    MetaConfig,
    activate_meta_ad,
    create_meta_ad_paused,
    create_meta_replacement_bundle_paused,
    get_meta_budget_state,
    set_meta_ad_status,
    set_meta_adset_daily_budget,
)


async def test_replacement_bundle_creates_one_budget_hierarchy_and_three_locale_ads(monkeypatch):
    created = []

    def create_step(**kwargs):
        progress = kwargs["progress"]
        locale = kwargs["locale"]
        if "campaign_id" not in progress:
            result = ("campaign_id", "campaign-1")
        elif "ad_set_id" not in progress:
            result = ("ad_set_id", "adset-1")
        elif f"creative_id:{locale}" not in progress:
            result = (f"creative_id:{locale}", f"creative-{locale}")
        else:
            result = (f"ad_id:{locale}", f"ad-{locale}")
        created.append(result[0])
        return result

    monkeypatch.setattr("peermarket_agent.meta_ads._sync_create_bundle_resource", create_step)
    persisted = AsyncMock()
    locale = MetaBundleLocale("body", "head", "desc", "LEARN_MORE", None)
    result = await create_meta_replacement_bundle_paused(
        config=_FULL_CONFIG,
        name="replacement",
        locales={key: locale for key in ("NL", "FR", "EN")},
        landing_page_url="https://peermarket.eu/",
        audience_profile_key="declutterers",
        daily_budget_eur=10,
        persist_progress=persisted,
    )
    assert created.count("campaign_id") == created.count("ad_set_id") == 1
    assert result.ad_ids == {"NL": "ad-NL", "FR": "ad-FR", "EN": "ad-EN"}
    assert persisted.await_count == 8


async def test_replacement_bundle_retry_reuses_durable_progress(monkeypatch):
    created = []

    def create_step(**kwargs):
        locale = kwargs["locale"]
        progress = kwargs["progress"]
        key = (
            f"creative_id:{locale}"
            if f"creative_id:{locale}" not in progress
            else f"ad_id:{locale}"
        )
        created.append(key)
        return key, key.replace(":", "-")

    monkeypatch.setattr("peermarket_agent.meta_ads._sync_create_bundle_resource", create_step)
    progress = {
        "campaign_id": "campaign-1",
        "ad_set_id": "adset-1",
        "creative_id:NL": "creative-NL",
        "ad_id:NL": "ad-NL",
    }
    locale = MetaBundleLocale("body", "head", "desc", "LEARN_MORE", None)
    await create_meta_replacement_bundle_paused(
        config=_FULL_CONFIG,
        name="replacement",
        locales={key: locale for key in ("NL", "FR", "EN")},
        landing_page_url="https://peermarket.eu/",
        audience_profile_key="declutterers",
        daily_budget_eur=10,
        progress=progress,
        persist_progress=AsyncMock(),
    )
    assert created == ["creative_id:FR", "ad_id:FR", "creative_id:EN", "ad_id:EN"]


_FULL_CONFIG = MetaConfig(
    app_id="111",
    app_secret="s",
    system_user_token="super-secret-token",
    ad_account_id="act_999",
    page_id="61592144690879",
)


def _patch_mutation_sdk(monkeypatch, *, ad_state=None, adset_state=None):
    api = object()
    init = MagicMock(return_value=api)
    ad = MagicMock()
    ad.api_get.return_value = ad_state or {
        "status": "PAUSED",
        "effective_status": "PAUSED",
    }
    adset = MagicMock()
    adset.api_get.return_value = adset_state or {
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "daily_budget": "1200",
    }
    ad_factory = MagicMock(return_value=ad)
    adset_factory = MagicMock(return_value=adset)
    monkeypatch.setattr("peermarket_agent.meta_ads._init_api", init)
    monkeypatch.setattr("peermarket_agent.meta_ads.Ad", ad_factory)
    monkeypatch.setattr("peermarket_agent.meta_ads.AdSet", adset_factory)
    return api, init, ad, adset, ad_factory, adset_factory


async def test_set_meta_ad_status_targets_exact_ad_binds_api_and_verifies(monkeypatch):
    api, _, ad, _, ad_factory, _ = _patch_mutation_sdk(monkeypatch)

    observed = await set_meta_ad_status(_FULL_CONFIG, "456", "PAUSED")

    ad_factory.assert_called_once_with("456", api=api)
    ad.api_update.assert_called_once_with(params={"status": "PAUSED"})
    ad.api_get.assert_called_once_with(fields=["status", "effective_status"])
    assert observed == {"status": "PAUSED", "effective_status": "PAUSED"}


@pytest.mark.parametrize("status", ["active", "DELETED", "", None, True])
async def test_set_meta_ad_status_rejects_invalid_status_before_api_init(monkeypatch, status):
    _, init, *_ = _patch_mutation_sdk(monkeypatch)

    with pytest.raises(ValueError, match="status"):
        await set_meta_ad_status(_FULL_CONFIG, "456", status)

    init.assert_not_called()


async def test_set_meta_ad_status_rejects_empty_id_before_api_init(monkeypatch):
    _, init, *_ = _patch_mutation_sdk(monkeypatch)

    with pytest.raises(ValueError, match="ad_id"):
        await set_meta_ad_status(_FULL_CONFIG, "", "PAUSED")

    init.assert_not_called()


async def test_set_meta_ad_status_raises_structured_error_on_verification_mismatch(
    monkeypatch,
):
    _patch_mutation_sdk(
        monkeypatch,
        ad_state={"status": "ACTIVE", "effective_status": "ACTIVE"},
    )

    with pytest.raises(MetaAdsError) as caught:
        await set_meta_ad_status(_FULL_CONFIG, "456", "PAUSED")

    assert caught.value.phase == "verify_ad_status"
    assert caught.value.resource_ids == {"ad_id": "456"}
    assert caught.value.observed_statuses == {
        "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"}
    }


@pytest.mark.parametrize("cents", [True, False, 0, -1, 1.5, "100"])
async def test_set_meta_adset_daily_budget_requires_positive_integer_before_api_init(
    monkeypatch, cents
):
    _, init, *_ = _patch_mutation_sdk(monkeypatch)

    with pytest.raises(ValueError, match="cents"):
        await set_meta_adset_daily_budget(_FULL_CONFIG, "123", cents)

    init.assert_not_called()


async def test_set_meta_adset_daily_budget_targets_exact_adset_and_verifies(monkeypatch):
    api, _, _, adset, _, adset_factory = _patch_mutation_sdk(monkeypatch)

    observed = await set_meta_adset_daily_budget(_FULL_CONFIG, "123", 1200)

    adset_factory.assert_called_once_with("123", api=api)
    adset.api_update.assert_called_once_with(params={"daily_budget": 1200})
    adset.api_get.assert_called_once_with(fields=["daily_budget"])
    assert observed == {"daily_budget": 1200}


async def test_set_meta_adset_daily_budget_mismatch_raises_structured_error(monkeypatch):
    _patch_mutation_sdk(monkeypatch, adset_state={"daily_budget": "1100"})

    with pytest.raises(MetaAdsError) as caught:
        await set_meta_adset_daily_budget(_FULL_CONFIG, "123", 1200)

    assert caught.value.phase == "verify_ad_set_daily_budget"
    assert caught.value.resource_ids == {"ad_set_id": "123"}
    assert caught.value.observed_statuses == {"ad_set": {"daily_budget": 1100}}


async def test_get_meta_budget_state_reads_bound_exact_resources(monkeypatch):
    api, _, ad, adset, ad_factory, adset_factory = _patch_mutation_sdk(monkeypatch)

    observed = await get_meta_budget_state(_FULL_CONFIG, {"ad_id": "456", "ad_set_id": "123"})

    ad_factory.assert_called_once_with("456", api=api)
    adset_factory.assert_called_once_with("123", api=api)
    ad.api_get.assert_called_once_with(fields=["status", "effective_status"])
    adset.api_get.assert_called_once_with(fields=["status", "effective_status", "daily_budget"])
    assert observed["ad"] == {"status": "PAUSED", "effective_status": "PAUSED"}
    assert observed["ad_set"]["daily_budget"] == 1200


@pytest.mark.parametrize(
    ("api_error_code", "http_status"),
    [(17, 400), (32, 400), (613, 400), (1, 429)],
)
@pytest.mark.parametrize("adapter", ["status", "budget", "read"])
async def test_mutation_error_preserves_structured_rate_limit_diagnostics(
    monkeypatch, adapter, api_error_code, http_status
):
    from facebook_business.exceptions import FacebookRequestError

    _, _, ad, adset, *_ = _patch_mutation_sdk(monkeypatch)
    sdk_error = FacebookRequestError(
        f"request token={_FULL_CONFIG.system_user_token}",
        request_context={"access_token": _FULL_CONFIG.system_user_token},
        http_status=http_status,
        http_headers={},
        body={
            "error": {
                "message": f"rate limited token={_FULL_CONFIG.system_user_token}",
                "code": api_error_code,
                "error_subcode": 2446079,
                "type": "OAuthException",
            }
        },
    )
    if adapter == "status":
        ad.api_update.side_effect = sdk_error
        mutation = set_meta_ad_status(_FULL_CONFIG, "456", "ACTIVE")
        expected_phase = "update_ad_status"
        expected_ids = {"ad_id": "456"}
    elif adapter == "budget":
        adset.api_update.side_effect = sdk_error
        mutation = set_meta_adset_daily_budget(_FULL_CONFIG, "123", 1200)
        expected_phase = "update_ad_set_daily_budget"
        expected_ids = {"ad_set_id": "123"}
    else:
        ad.api_get.side_effect = sdk_error
        mutation = get_meta_budget_state(_FULL_CONFIG, {"ad_id": "456", "ad_set_id": "123"})
        expected_phase = "get_budget_state"
        expected_ids = {"ad_id": "456", "ad_set_id": "123"}

    with pytest.raises(MetaAdsError) as caught:
        await mutation

    assert caught.value.phase == expected_phase
    assert caught.value.resource_ids == expected_ids
    assert caught.value.api_error_code == api_error_code
    assert caught.value.api_error_subcode == 2446079
    assert caught.value.http_status == http_status
    assert caught.value.api_error_type == "OAuthException"
    assert _FULL_CONFIG.system_user_token not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_generic_mutation_error_remains_sanitized_and_unstructured(monkeypatch):
    _, _, ad, *_ = _patch_mutation_sdk(monkeypatch)
    ad.api_update.side_effect = RuntimeError(
        f"generic failure token={_FULL_CONFIG.system_user_token}"
    )

    with pytest.raises(MetaAdsError) as caught:
        await set_meta_ad_status(_FULL_CONFIG, "456", "ACTIVE")

    assert "generic failure" in str(caught.value)
    assert _FULL_CONFIG.system_user_token not in str(caught.value)
    assert caught.value.api_error_code is None
    assert caught.value.api_error_subcode is None
    assert caught.value.http_status is None
    assert caught.value.api_error_type is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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
    config = MetaConfig(
        app_id="", app_secret="", system_user_token="", ad_account_id="", page_id=""
    )
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


async def test_create_paused_ad_uses_configured_page_identity(monkeypatch):
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

    creative_params = fake.create_ad_creative.call_args.kwargs["params"]
    assert creative_params["object_story_spec"]["page_id"] == "61592144690879"


async def test_create_paused_ad_disabled_when_page_id_missing():
    config = MetaConfig(
        app_id="111",
        app_secret="s",
        system_user_token="t",
        ad_account_id="act_999",
        page_id="",
    )
    with pytest.raises(MetaAdsDisabled, match="page_id"):
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


async def test_create_paused_ad_uses_supported_billing_event(monkeypatch):
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
    params = fake.create_ad_set.call_args.kwargs["params"]
    assert params["billing_event"] == "IMPRESSIONS"
    assert params["optimization_goal"] == "LINK_CLICKS"


@pytest.mark.parametrize("audience_profile", ["declutterers", "trust_conscious_locals"])
async def test_create_paused_ad_explicitly_disables_advantage_audience(
    monkeypatch, audience_profile
):
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
        audience_profile_key=audience_profile,
        daily_budget_eur=5,
    )
    targeting = fake.create_ad_set.call_args.kwargs["params"]["targeting"]
    assert targeting["targeting_automation"] == {"advantage_audience": 0}


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


async def test_create_failure_retains_created_ids_and_rolls_back(monkeypatch):
    _patch_meta_sdk(monkeypatch, raise_on="ad")
    pause_mock = MagicMock(return_value={})
    monkeypatch.setattr("peermarket_agent.meta_ads._sync_pause", pause_mock)

    with pytest.raises(MetaAdsError) as caught:
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

    assert caught.value.phase == "create_ad"
    assert caught.value.resource_ids == {
        "campaign_id": "c1",
        "ad_set_id": "as1",
        "creative_id": "cr1",
    }
    assert caught.value.rollback_errors == {}
    pause_mock.assert_called_once_with(
        _FULL_CONFIG,
        {"campaign_id": "c1", "ad_set_id": "as1", "creative_id": "cr1"},
    )


async def test_create_paused_ad_preserves_actionable_api_error_details(monkeypatch):
    from facebook_business.exceptions import FacebookRequestError

    error = FacebookRequestError(
        "Request failed",
        request_context={},
        http_status=400,
        http_headers={},
        body={
            "error": {
                "message": "Invalid parameter",
                "code": 100,
                "error_subcode": 18157520,
                "error_user_title": "Invalid Page",
                "error_user_msg": "Select a Page connected to this ad account.",
            }
        },
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_ads.FacebookAdsApi.init",
        lambda *a, **kw: None,
    )
    fake_account = MagicMock()
    fake_account.create_campaign.side_effect = error
    monkeypatch.setattr(
        "peermarket_agent.meta_ads.AdAccount",
        lambda *a, **kw: fake_account,
    )

    with pytest.raises(MetaAdsError) as exc_info:
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

    message = str(exc_info.value)
    assert "Invalid parameter" in message
    assert "code=100" in message
    assert "subcode=18157520" in message
    assert "Invalid Page" in message
    assert "Select a Page connected to this ad account." in message


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
    activation_error_message=None,
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
                raise RuntimeError(
                    activation_error_message or f"failed to activate {self.resource_type}"
                )
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


async def test_activation_error_does_not_chain_credential_bearing_cause(monkeypatch):
    statuses = {
        "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad_set": {"status": "PAUSED", "effective_status": "PAUSED"},
        "ad": {"status": "PAUSED", "effective_status": "PAUSED"},
    }
    _patch_resources(
        monkeypatch,
        statuses,
        fail_on="ad_set",
        activation_error_message=f"request token={_FULL_CONFIG.system_user_token}",
    )

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(
            _FULL_CONFIG,
            {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"},
        )

    chained_traceback = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert caught.value.__cause__ is None
    assert _FULL_CONFIG.system_user_token not in chained_traceback


async def test_activation_error_redacts_short_credentials_without_corrupting_words(
    monkeypatch,
):
    config = MetaConfig(
        app_id="111",
        app_secret="s3",
        system_user_token="t4",
        ad_account_id="act_999",
        page_id="61592144690879",
    )
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
        rollback_error_message="app_secret=s3; token=t4; status stays readable",
    )

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(
            config,
            {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"},
        )

    assert caught.value.rollback_errors == {
        "ad": "app_secret=[REDACTED]; token=[REDACTED]; status stays readable"
    }


async def test_activation_error_survives_rollback_setup_failure(monkeypatch):
    statuses = {
        "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
        "ad_set": {"status": "PAUSED", "effective_status": "PAUSED"},
        "ad": {"status": "PAUSED", "effective_status": "PAUSED"},
    }
    _patch_resources(monkeypatch, statuses, fail_on="ad_set")
    init_calls = 0

    def fail_rollback_init(config):
        nonlocal init_calls
        init_calls += 1
        if init_calls == 3:
            raise RuntimeError(
                f"rollback token={_FULL_CONFIG.system_user_token} could not initialize"
            )

    monkeypatch.setattr("peermarket_agent.meta_ads._init_api", fail_rollback_init)
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(_FULL_CONFIG, ids)

    error = caught.value
    assert error.phase == "activate_ad_set"
    assert error.resource_ids == ids
    assert error.observed_statuses == statuses
    assert error.rollback_errors == {"setup": "rollback token=[REDACTED] could not initialize"}
    assert error.__cause__ is None
    chained_traceback = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert _FULL_CONFIG.system_user_token not in chained_traceback


async def test_initial_activation_setup_failure_is_structured_and_sanitized(monkeypatch):
    def fail_init(config):
        raise RuntimeError(f"initialization token={_FULL_CONFIG.system_user_token} unavailable")

    monkeypatch.setattr("peermarket_agent.meta_ads._init_api", fail_init)
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}

    with pytest.raises(MetaAdsError) as caught:
        await activate_meta_ad(_FULL_CONFIG, ids)

    error = caught.value
    assert error.phase == "setup"
    assert error.resource_ids == ids
    assert error.observed_statuses == {}
    assert error.rollback_errors == {"setup": "initialization token=[REDACTED] unavailable"}
    assert error.__cause__ is None
    assert error.__context__ is None
    chained_traceback = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert _FULL_CONFIG.system_user_token not in chained_traceback
