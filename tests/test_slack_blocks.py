"""Block Kit builders render structured reports without raw JSON dumps."""

import json

from peermarket_agent.slack_blocks import (
    autonomy_audit_blocks,
    daily_summary_blocks,
    hourly_alert_blocks,
)

_AUDIT_PAYLOAD = {
    "audit": "autonomy",
    "campaign_id": "120249125021520342",
    "outcome": "shadow",
    "decision": "observe",
    "reason": "not_comparable",
    "experiment_id": None,
    "detail": "decision persisted; no action queued",
    "thresholds": {
        "cooldown_hours": 24,
        "min_impressions": 1000,
        "min_landing_page_views": 30,
        "min_registrations": 10,
    },
    "evidence": [
        {"variant_id": "156", "impressions": 11303, "landing_page_views": 42, "registrations": 0}
    ],
    "affected_ads": [
        {"publication_id": 1, "ad_set_id": "120249125021910342", "ad_id": "120249125024900342"}
    ],
    "budgets": {"previous_cents": None, "new_cents": None},
    "rollback": {"needed": False, "result": "not_required"},
    "next_evaluation_at": "2026-07-18T22:00:00+00:00",
    "replacement_result": None,
}


def test_autonomy_blocks_start_with_header_and_contain_no_json_dumps() -> None:
    blocks = autonomy_audit_blocks(_AUDIT_PAYLOAD)
    assert blocks[0]["type"] == "header"
    assert "shadow" in blocks[0]["text"]["text"]
    rendered = json.dumps(blocks)
    assert "{'" not in rendered and '{\\"' not in rendered  # no python/json dict dumps in copy
    assert "11,303" in rendered  # formatted numbers
    assert "not_comparable" in rendered
    assert "2026-07-18" in rendered


def test_autonomy_blocks_show_variant_metrics_as_fields() -> None:
    blocks = autonomy_audit_blocks(_AUDIT_PAYLOAD)
    fields_sections = [b for b in blocks if b.get("fields")]
    assert fields_sections, "expected a fields section for variant samples"
    field_text = fields_sections[0]["fields"][0]["text"]
    assert "156" in field_text and "42" in field_text


def test_autonomy_blocks_survive_sparse_payload() -> None:
    assert autonomy_audit_blocks({})[0]["type"] == "header"


def test_autonomy_blocks_survive_none_payload() -> None:
    blocks = autonomy_audit_blocks(None)
    assert isinstance(blocks, list) and blocks
    assert blocks[0]["type"] == "header"


def test_daily_summary_blocks_survive_none_message() -> None:
    blocks = daily_summary_blocks(None)
    assert isinstance(blocks, list) and blocks


def test_hourly_alert_blocks_survive_none_message() -> None:
    blocks = hourly_alert_blocks(None)
    assert isinstance(blocks, list) and blocks


def test_daily_summary_blocks_split_title_and_publications() -> None:
    message = (
        "Daily campaign evidence summary (descriptive observations only)\n"
        "• Publication #7 — approved budget 2000 cents; spend 150 cents; impressions 11303"
    )
    blocks = daily_summary_blocks(message)
    assert blocks[0]["type"] == "header"
    body = blocks[1]["text"]["text"]
    assert "Publication #7" in body
    assert "; " not in body  # semicolon runs become newlines


def test_hourly_alert_blocks_wrap_message() -> None:
    blocks = hourly_alert_blocks("Draft #3: Meta delivery problem: no_delivery")
    assert blocks[0]["type"] == "section"
    assert "no_delivery" in blocks[0]["text"]["text"]
    assert "⚠️" in blocks[0]["text"]["text"]
    recovered = hourly_alert_blocks("Meta delivery recovered from no_delivery")
    assert "✅" in recovered[0]["text"]["text"]
