"""Read-only production performance verifier contracts."""

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.cli_performance import _snapshot_is_fresh, cli, readonly_connection
from peermarket_agent.config import Settings
from peermarket_agent.meta_insights import MetaInsightSnapshot


@pytest.fixture
def settings():
    return SimpleNamespace(
        agent_db_url="postgresql+asyncpg://unused",
        peermarket_prod_db_readonly_url="postgresql+asyncpg://readonly",
        meta_app_id="app",
        meta_app_secret="secret",
        meta_system_user_token="token",
        meta_ad_account_id="act_1",
        meta_page_id="page",
        meta_insights_enabled=True,
        peermarket_attribution_enabled=False,
        meta_insights_lookback_days=3,
        meta_no_delivery_grace_hours=2,
        learning_min_impressions=1000,
        learning_min_landing_page_views=30,
        learning_min_registrations=10,
        meta_account_timezone="Europe/Brussels",
        performance_snapshot_max_age_hours=2,
    )


def test_verify_reports_sanitized_sources_without_mutation(monkeypatch, settings):
    now = datetime.now(UTC)
    publication = {
        "id": 9,
        "draft_id": 156,
        "state": "active",
        "external_ids": {"ad_id": "ad-1", "campaign_id": "campaign-1"},
        "performance": {"meta": {"last_successful_retrieval": now.isoformat()}},
    }
    snapshot = MetaInsightSnapshot(
        ad_id="ad-1",
        window_start=date.today() - timedelta(days=2),
        window_stop=date.today(),
        retrieved_at=now,
        spend_cents=125,
        impressions=42,
        reach=30,
        clicks=4,
        inline_link_clicks=3,
        outbound_clicks=2,
        landing_page_views=1,
        ctr=None,
        cpc_cents=None,
        cpm_cents=None,
        frequency=None,
        actions={},
    )
    read_publication = AsyncMock(return_value=publication)
    read_statuses = AsyncMock(
        return_value={"ad": {"status": "ACTIVE", "effective_status": "ACTIVE"}}
    )
    read_insights = AsyncMock(return_value=snapshot)
    read_attribution = AsyncMock()
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_publication", read_publication)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_meta_statuses", read_statuses)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_meta_insights", read_insights)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_attribution", read_attribution)

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "156"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["meta_available"] is True
    assert report["attribution_available"] is False
    assert report["attribution_status"] == "disabled"
    assert report["meta_counts"] == {"impressions": 42, "landing_page_views": 1}
    assert report["publication"] == {
        "draft_id": 156,
        "external_ids": {"ad_id": "ad-1", "campaign_id": "campaign-1"},
        "id": 9,
        "state": "active",
    }
    assert "secret" not in result.output.lower()
    assert "token" not in result.output.lower()
    read_publication.assert_awaited_once()
    read_statuses.assert_awaited_once()
    read_insights.assert_awaited_once()
    read_attribution.assert_not_awaited()


def test_verify_fails_safely_for_absent_publication_without_external_reads(monkeypatch, settings):
    read_statuses = AsyncMock()
    read_insights = AsyncMock()
    read_attribution = AsyncMock()
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_publication", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("peermarket_agent.cli_performance.read_meta_statuses", read_statuses)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_meta_insights", read_insights)
    monkeypatch.setattr("peermarket_agent.cli_performance.read_attribution", read_attribution)

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "999"])

    assert result.exit_code == 1
    report = json.loads(result.output.splitlines()[0])
    assert report["publication_exists"] is False
    assert "publication_missing" in result.output
    read_statuses.assert_not_awaited()
    read_insights.assert_not_awaited()
    read_attribution.assert_not_awaited()


def test_verify_sanitizes_publication_read_failure(monkeypatch, settings):
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_publication",
        AsyncMock(side_effect=RuntimeError("postgresql://admin:password@prod/raw")),
    )

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "156"])

    assert result.exit_code == 1
    assert "publication_read_failed" in result.output
    assert "password" not in result.output
    assert "admin" not in result.output


@pytest.mark.parametrize(
    ("meta_enabled", "attribution_enabled", "failed_reader", "failure"),
    [
        (True, False, "read_meta_insights", "meta_unavailable"),
        (False, True, "read_attribution", "attribution_unavailable"),
    ],
)
def test_verify_fails_when_enabled_source_read_fails(
    monkeypatch, settings, meta_enabled, attribution_enabled, failed_reader, failure
):
    settings.meta_insights_enabled = meta_enabled
    settings.peermarket_attribution_enabled = attribution_enabled
    publication = {
        "id": 9,
        "draft_id": 156,
        "state": "active",
        "external_ids": {"ad_id": "ad-1"},
        "performance": {"meta": {"last_successful_retrieval": datetime.now(UTC).isoformat()}},
    }
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_publication",
        AsyncMock(return_value=publication),
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_statuses", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_insights", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_attribution", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        f"peermarket_agent.cli_performance.{failed_reader}",
        AsyncMock(side_effect=RuntimeError("token=raw-secret")),
    )

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "156"])

    assert result.exit_code == 1
    assert failure in result.output
    assert "raw-secret" not in result.output


def test_verify_fails_when_enabled_meta_snapshot_is_stale(monkeypatch, settings):
    publication = {
        "id": 9,
        "draft_id": 156,
        "state": "active",
        "external_ids": {"ad_id": "ad-1"},
        "performance": {
            "meta": {
                "last_successful_retrieval": (
                    datetime.now(UTC) - timedelta(hours=2, seconds=1)
                ).isoformat()
            }
        },
    }
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_publication",
        AsyncMock(return_value=publication),
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_statuses",
        AsyncMock(return_value={"ad": {"status": "ACTIVE"}}),
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_insights",
        AsyncMock(return_value=SimpleNamespace(impressions=1, landing_page_views=1)),
    )

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "156"])

    assert result.exit_code == 1
    assert "snapshot_stale" in result.output


def test_verify_uses_dedicated_snapshot_freshness_and_exact_draft_campaign(monkeypatch, settings):
    settings.meta_no_delivery_grace_hours = 99
    settings.performance_snapshot_max_age_hours = 1
    settings.peermarket_attribution_enabled = True
    publication = {
        "id": 9,
        "draft_id": 156,
        "state": "active",
        "external_ids": {"ad_id": "ad-1"},
        "performance": {
            "meta": {
                "last_successful_retrieval": (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            }
        },
    }
    attribution = [
        SimpleNamespace(utm_content="draft-156", event_type="registration_completed", count=2),
        SimpleNamespace(utm_content="draft-999", event_type="registration_completed", count=50),
    ]
    monkeypatch.setattr("peermarket_agent.cli_performance.get_settings", lambda: settings)
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_publication", AsyncMock(return_value=publication)
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_statuses", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_meta_insights",
        AsyncMock(return_value=SimpleNamespace(impressions=1, landing_page_views=1)),
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_performance.read_attribution", AsyncMock(return_value=attribution)
    )

    result = CliRunner().invoke(cli, ["verify", "--draft-id", "156"])

    assert result.exit_code == 1
    report = json.loads(result.output.splitlines()[0])
    assert report["attribution_counts"] == {"registration_completed": 2}
    assert "snapshot_stale" in result.output


def test_snapshot_freshness_uses_aware_exact_boundary_and_clock_skew():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)

    assert _snapshot_is_fresh(now - timedelta(hours=2), now, max_age_hours=2)
    assert not _snapshot_is_fresh(now - timedelta(hours=2, microseconds=1), now, max_age_hours=2)
    assert _snapshot_is_fresh(now + timedelta(minutes=5), now, max_age_hours=2)
    assert not _snapshot_is_fresh(now + timedelta(minutes=5, microseconds=1), now, max_age_hours=2)
    assert not _snapshot_is_fresh(now.replace(tzinfo=None), now, max_age_hours=2)


@pytest.fixture
async def postgres_engine():
    dsn = os.environ.get("AGENT_DB_URL")
    if not dsn:
        pytest.skip("AGENT_DB_URL disposable test DSN is not configured")
    engine = create_async_engine(dsn, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_readonly_connection_enforces_postgres_read_only(postgres_engine):
    with pytest.raises(DBAPIError):
        async with readonly_connection(postgres_engine) as connection:
            mode = (await connection.execute(text("SHOW transaction_read_only"))).scalar_one()
            assert mode == "on"
            await connection.execute(text("CREATE TABLE verifier_must_not_write (id INT)"))

    async with postgres_engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("META_INSIGHTS_LOOKBACK_DAYS", "0"),
        ("META_INSIGHTS_LOOKBACK_DAYS", "31"),
        ("META_NO_DELIVERY_GRACE_HOURS", "-1"),
        ("META_NO_DELIVERY_GRACE_HOURS", "169"),
        ("LEARNING_MIN_IMPRESSIONS", "0"),
        ("LEARNING_MIN_LANDING_PAGE_VIEWS", "0"),
        ("LEARNING_MIN_REGISTRATIONS", "0"),
    ],
)
def test_performance_controls_have_safe_bounds(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert any(item["loc"] == (name.lower(),) for item in error.value.errors())


def test_verifier_source_has_no_meta_mutation_surface():
    source = (
        Path(__file__).parents[1] / "src" / "peermarket_agent" / "cli_performance.py"
    ).read_text()

    assert "activate_meta_ad" not in source
    assert "pause_meta_ad" not in source
    assert "api_update" not in source
    assert 'text("UPDATE ' not in source
