"""Canonical hourly-performance to autonomy-policy evidence contracts."""

from datetime import UTC, datetime

from peermarket_agent.autonomy.contracts import DecisionKind
from peermarket_agent.autonomy.executor import _replacement_source
from peermarket_agent.autonomy.snapshot import (
    build_autonomy_basis,
    build_autonomy_snapshot,
    build_policy_decision,
)

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


def test_real_performance_namespaces_build_an_executable_policy_snapshot():
    variants = [
        {
            "variant_id": str(index),
            "publication_id": index,
            "channel": "meta",
            "objective": "OUTCOME_TRAFFIC",
            "language": "MULTI",
            "audience": "declutterers",
            "creative_dimension": "hook",
            "window_definition": "rolling-1-inclusive-calendar-days",
            "impressions": 1000,
            "landing_page_views": 30,
            "registrations": registrations,
        }
        for index, registrations in ((1, 20), (2, 10))
    ]
    source = {
        "draft_id": 1,
        "publication_id": 1,
        "campaign_id": "10",
        "experiment_id": "experiment-1",
        "changed_dimension": "hook",
        "locales": {
            locale: {
                "locale": locale,
                "hook": "hook",
                "body": "body",
                "headline": "headline",
                "description": "description",
                "cta_label": "Learn More",
            }
            for locale in ("NL", "FR", "EN")
        },
        "audience_profile_key": "declutterers",
        "image_prompt": "real screenshot",
        "asset_path": "/tmp/source.png",
        "daily_budget_eur": 10,
        "landing_page_url": "https://peermarket.eu/",
        "objective": "OUTCOME_TRAFFIC",
        "current_meta_ids": {
            "campaign_id": "10",
            "ad_set_id": "20",
            "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
            "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
        },
    }
    publication = {
        "external_ids": {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
        "approved_budget_cents": 1000,
        "performance": {
            "meta": {
                "latest": {
                    "utc_alignment": {
                        "start": "2026-07-16T12:00:00+00:00",
                        "stop_exclusive": "2026-07-17T12:00:00+00:00",
                    }
                },
                "last_successful_retrieval": NOW.isoformat(),
                "error": None,
                "restated": False,
            },
            "delivery": {"condition": "healthy"},
            "attribution": {"available": True},
        },
    }
    publication["performance"]["autonomy_basis"] = build_autonomy_basis(
        publication, publication["performance"]
    )
    limits = {
        "performance_snapshot_max_age_hours": 2,
        "learning_min_impressions": 1000,
        "learning_min_landing_page_views": 30,
        "learning_min_registrations": 10,
        "meta_autonomy_cooldown_hours": 24,
        "meta_autonomy_max_test_days": 7,
        "meta_autonomy_max_replacements_24h": 1,
        "meta_autonomy_max_increase_percent": 20,
        "meta_autonomy_max_daily_budget_eur": 20,
        "meta_no_delivery_grace_hours": 2,
        "meta_account_timezone": "Europe/Brussels",
    }
    decision = build_policy_decision(
        publication,
        variants,
        replacement_source=source,
        history=(),
        limits=limits,
        now=NOW,
    )
    assert decision.kind is DecisionKind.REPLACE
    assert decision.evidence["source"] == source
    parsed = _replacement_source(decision)
    assert parsed.current_meta_ids == source["current_meta_ids"]
    assert parsed.publication_id == 1


def test_canonical_digest_freezes_persisted_full_ids_and_budget_not_mutable_publication():
    original = {
        "external_ids": {
            "campaign_id": "10",
            "ad_set_id": "20",
            "ad_id": "31",
            "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
        },
        "approved_budget_cents": 1000,
    }
    performance = {
        "meta": {
            "latest": {
                "utc_alignment": {
                    "start": "2026-07-16T12:00:00+00:00",
                    "stop_exclusive": "2026-07-17T12:00:00+00:00",
                }
            },
            "last_successful_retrieval": NOW.isoformat(),
            "error": None,
            "restated": False,
        },
        "delivery": {"condition": "healthy"},
        "attribution": {"available": True},
    }
    performance["autonomy_basis"] = build_autonomy_basis(original, performance)
    changed = {
        "external_ids": {"campaign_id": "10", "ad_set_id": "999", "ad_id": "998"},
        "approved_budget_cents": 2000,
        "performance": performance,
    }
    first = build_autonomy_snapshot(
        original | {"performance": performance}, [{"variant_id": "1"}], replacement_source=None
    )
    second = build_autonomy_snapshot(changed, [{"variant_id": "1"}], replacement_source=None)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["frozen_basis"]["external_ids"] == original["external_ids"]
    assert second["frozen_basis"]["approved_budget_cents"] == 1000
