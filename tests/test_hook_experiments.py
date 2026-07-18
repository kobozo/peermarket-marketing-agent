"""Pure deterministic multilingual hook experiment generation contracts."""

from copy import deepcopy
from dataclasses import replace

import pytest

from peermarket_agent.autonomy import hook_experiments
from peermarket_agent.autonomy.contracts import HookVariant, thaw_json
from peermarket_agent.autonomy.hook_experiments import (
    build_hook_experiment,
    build_hook_proposal,
    validate_hook_experiment,
)


def test_build_hook_proposal_fills_missing_multilingual_baseline():
    draft = {
        "id": 156,
        "campaign_id": "120249125021520342",
        "ad_set_id": "adset-1",
        "landing_page_url": "https://peermarket.eu/",
        "fixed_identity": {"audience": "same"},
        "title": "Sell safely on Peermarket",
    }
    proposal = build_hook_proposal(draft, "warm and practical")

    assert set(proposal) == {"NL", "FR", "EN"}
    assert all(len(proposal[locale]["hook"]) > 10 for locale in proposal)
    assert len({proposal[locale]["hook"] for locale in proposal}) == 3


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
    validate_hook_experiment(experiment, _draft(), "PeerMarket Belgian voice", 42)


def test_stable_ids_commit_to_baseline_copy_and_hook_catalog_version(monkeypatch):
    original = build_hook_experiment(_draft(), "voice", seed="commitment")
    changed_draft = deepcopy(_draft())
    changed_draft["language_bundles"]["NL"]["body"] = "Andere vaste tekst"
    changed_copy = build_hook_experiment(changed_draft, "voice", seed="commitment")
    assert changed_copy.experiment_id != original.experiment_id
    assert changed_copy.variants[0].variant_id != original.variants[0].variant_id

    monkeypatch.setattr(hook_experiments, "HOOK_CATALOG_VERSION", "hook-catalog-v2")
    changed_catalog = build_hook_experiment(_draft(), "voice", seed="commitment")
    assert changed_catalog.experiment_id != original.experiment_id
    assert changed_catalog.variants[0].variant_id != original.variants[0].variant_id


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
        validate_hook_experiment(
            experiment,
            {**_draft(), "fixed_identity": {**_draft()["fixed_identity"], "audience": "other"}},
            "voice",
            7,
        )

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
        validate_hook_experiment(broken, _draft(), "voice", 7)

    bundles = thaw_json(variant.language_bundles)
    bundles["EN"]["hook"] = "Placeholder"
    missing = replace(literal, language_bundles=bundles)
    broken = replace(experiment, variants=(missing, *experiment.variants[1:]))
    with pytest.raises(ValueError, match="native|literal|language"):
        validate_hook_experiment(broken, _draft(), "voice", 7)


def test_validation_recomputes_commitment_and_requires_each_locale_hook_to_be_distinct():
    draft = _draft()
    experiment = build_hook_experiment(draft, "voice", seed=9)
    arbitrary = replace(
        experiment,
        experiment_id="draft-156-hook-0000000000000000",
        variants=tuple(
            replace(
                item,
                experiment_id="draft-156-hook-0000000000000000",
                variant_id=f"draft-156-hook-0000000000000000:{number:02d}",
            )
            for number, item in enumerate(experiment.variants, 1)
        ),
    )
    with pytest.raises(ValueError, match="derivation|commitment"):
        validate_hook_experiment(arbitrary, draft, "voice", 9)

    bundles = thaw_json(experiment.variants[1].language_bundles)
    bundles["NL"]["hook"] = experiment.variants[0].language_bundles["NL"]["hook"]
    duplicate_nl = replace(experiment.variants[1], language_bundles=bundles)
    duplicate = replace(
        experiment,
        variants=(experiment.variants[0], duplicate_nl, experiment.variants[2]),
    )
    with pytest.raises(ValueError, match="distinct.*NL|NL.*distinct"):
        validate_hook_experiment(duplicate, draft, "voice", 9)
