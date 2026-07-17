"""Pure deterministic multilingual hook experiment generation contracts."""

from dataclasses import replace

import pytest

from peermarket_agent.autonomy.contracts import HookVariant, thaw_json
from peermarket_agent.autonomy.hook_experiments import (
    build_hook_experiment,
    validate_hook_experiment,
)


def _draft():
    return {
        "id": 156,
        "campaign_id": "120249125021520342",
        "ad_set_id": "120249125021520343",
        "landing_page_url": "https://peermarket.eu/signup",
        "fixed_identity": {
            "audience": "declutterers",
            "optimization": "LANDING_PAGE_VIEWS",
            "format": "single_image",
            "visual": "asset-1",
            "delivery": "lowest_cost",
        },
        "language_bundles": {
            locale: {
                "hook": "baseline",
                "body": f"{locale} trusted marketplace body",
                "headline": f"{locale} trusted headline",
                "description": f"{locale} trusted description",
                "cta_label": "Learn More",
            }
            for locale in ("NL", "FR", "EN")
        },
    }


def test_build_hook_experiment_replays_deterministically_with_stable_ordered_ids():
    first = build_hook_experiment(_draft(), "clear, practical and warm", seed="launch-1")
    second = build_hook_experiment(_draft(), "clear, practical and warm", seed="launch-1")
    assert first == second
    assert first.experiment_id.startswith("draft-156-hook-")
    assert [item.variant_id for item in first.variants] == [
        f"{first.experiment_id}:01",
        f"{first.experiment_id}:02",
        f"{first.experiment_id}:03",
    ]


def test_generation_has_three_distinct_native_hooks_and_one_fixed_identity():
    experiment = build_hook_experiment(_draft(), "PeerMarket Belgian voice", seed=42)
    assert len(experiment.variants) == 3
    assert (
        len(
            {
                tuple(v.language_bundles[locale]["hook"] for locale in ("NL", "FR", "EN"))
                for v in experiment.variants
            }
        )
        == 3
    )
    assert all(set(v.language_bundles) == {"NL", "FR", "EN"} for v in experiment.variants)
    assert all(v.fixed_identity == experiment.fixed_identity for v in experiment.variants)
    assert all(v.landing_page_url == experiment.landing_page_url for v in experiment.variants)
    validate_hook_experiment(experiment, _draft()["fixed_identity"])


def test_generation_changes_only_hook_and_never_embeds_brand_voice_or_tokens():
    draft = _draft()
    secret = "Bearer secret-token-must-not-leak"
    experiment = build_hook_experiment(draft, secret, seed="same")
    for variant in experiment.variants:
        for locale in ("NL", "FR", "EN"):
            for field in ("body", "headline", "description", "cta_label"):
                assert (
                    variant.language_bundles[locale][field]
                    == draft["language_bundles"][locale][field]
                )
    assert secret not in repr(thaw_json(experiment.fixed_identity))
    assert secret not in repr([thaw_json(v.language_bundles) for v in experiment.variants])


def test_validation_rejects_fixed_identity_drift_and_literal_or_missing_language_hooks():
    experiment = build_hook_experiment(_draft(), "voice", seed=7)
    with pytest.raises(ValueError, match="fixed identity"):
        validate_hook_experiment(experiment, {**_draft()["fixed_identity"], "audience": "other"})

    variant = experiment.variants[0]
    bundles = thaw_json(variant.language_bundles)
    bundles["FR"]["hook"] = bundles["NL"]["hook"]
    literal = HookVariant(
        variant_id=variant.variant_id,
        experiment_id=variant.experiment_id,
        campaign_id=variant.campaign_id,
        ad_set_id=variant.ad_set_id,
        landing_page_url=variant.landing_page_url,
        changed_dimension="hook",
        fixed_identity=thaw_json(variant.fixed_identity),
        language_bundles=bundles,
    )
    broken = replace(experiment, variants=(literal, *experiment.variants[1:]))
    with pytest.raises(ValueError, match="native|literal|language"):
        validate_hook_experiment(broken, _draft()["fixed_identity"])

    bundles = thaw_json(variant.language_bundles)
    bundles["EN"]["hook"] = "Placeholder"
    missing = replace(literal, language_bundles=bundles)
    broken = replace(experiment, variants=(missing, *experiment.variants[1:]))
    with pytest.raises(ValueError, match="native|literal|language"):
        validate_hook_experiment(broken, _draft()["fixed_identity"])
