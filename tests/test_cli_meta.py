"""Safety contract for the Meta reconciliation operator command."""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.cli_meta import cli, reconcile_draft
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.publications import (
    MetaPublication,
    get_meta_publication,
    upsert_meta_publication,
)

IDS = {
    "campaign_id": "campaign-1",
    "ad_set_id": "adset-1",
    "creative_id": "creative-1",
    "ad_id": "ad-1",
}


@pytest.fixture
async def disposable_draft():
    """Give this module its own schema on the configured disposable test DSN."""
    dsn = os.environ.get("AGENT_DB_URL")
    if not dsn:
        pytest.skip("AGENT_DB_URL disposable test DSN is not configured")
    schema = f"test_cli_meta_{uuid4().hex}"
    admin = create_async_engine(dsn, future=True)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        dsn,
        future=True,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        await run_migrations(engine)
        async with engine.begin() as connection:
            action_type_id = (
                await connection.execute(
                    text(
                        "INSERT INTO action_types (name, risk_tier, default_autonomy) "
                        "VALUES ('meta_ad_creative', 'high', 'propose') RETURNING id"
                    )
                )
            ).scalar_one()
            draft_id = (
                await connection.execute(
                    text(
                        "INSERT INTO drafts (action_type_id, channel, language, status) "
                        "VALUES (:action_type_id, 'meta', 'NL', 'approved') RETURNING id"
                    ),
                    {"action_type_id": action_type_id},
                )
            ).scalar_one()
        yield engine, draft_id
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def test_reconcile_cli_requires_every_resource_id():
    result = CliRunner().invoke(
        cli,
        ["reconcile-draft", "--draft-id", "156", "--campaign-id", "campaign-1"],
    )

    assert result.exit_code == 2
    assert "Missing option '--adset-id'" in result.output


async def test_dry_run_displays_state_and_status_without_writing_or_dispatching(monkeypatch):
    publication = MetaPublication(
        draft_id=42,
        state="failed",
        external_ids=IDS,
        external_statuses={"campaign": {"status": "PAUSED"}},
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_meta.get_meta_publication",
        AsyncMock(return_value=publication),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr("peermarket_agent.cli_meta.process_approved_meta_draft", dispatch)

    lines = await reconcile_draft(
        engine=object(),
        draft_id=42,
        supplied_ids=IDS,
        settings=object(),
        notifier=object(),
        dry_run=True,
    )

    assert lines == [
        "Draft #42 reconciliation dry run",
        "Publication state: failed",
        'Observed statuses: {"campaign": {"status": "PAUSED"}}',
        "No changes made.",
    ]
    dispatch.assert_not_awaited()


async def test_dry_run_refuses_conflicting_stored_id(monkeypatch):
    publication = MetaPublication(
        draft_id=42, state="failed", external_ids={**IDS, "ad_id": "different-ad"}
    )
    monkeypatch.setattr(
        "peermarket_agent.cli_meta.get_meta_publication",
        AsyncMock(return_value=publication),
    )

    with pytest.raises(ValueError, match="supplied ad_id .* conflicts"):
        await reconcile_draft(
            engine=object(),
            draft_id=42,
            supplied_ids=IDS,
            settings=object(),
            notifier=object(),
            dry_run=True,
        )


async def test_reconciliation_dispatches_ids_to_production_pipeline(monkeypatch):
    monkeypatch.setattr(
        "peermarket_agent.cli_meta.get_meta_publication", AsyncMock(return_value=None)
    )
    dispatch = AsyncMock()
    monkeypatch.setattr("peermarket_agent.cli_meta.process_approved_meta_draft", dispatch)
    engine, settings, notifier = object(), object(), object()

    await reconcile_draft(
        engine=engine,
        draft_id=42,
        supplied_ids=IDS,
        settings=settings,
        notifier=notifier,
        dry_run=False,
    )

    dispatch.assert_awaited_once_with(
        engine=engine,
        draft_id=42,
        settings=settings,
        notifier=notifier,
        reconciliation_ids=IDS,
    )


async def test_reconciliation_persists_ids_on_disposable_database(monkeypatch, disposable_draft):
    engine, draft_id = disposable_draft
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", AsyncMock())

    await reconcile_draft(
        engine=engine,
        draft_id=draft_id,
        supplied_ids=IDS,
        settings=object(),
        notifier=object(),
        dry_run=False,
    )

    publication = await get_meta_publication(engine, draft_id)
    assert publication is not None
    assert publication.state == "created"
    assert publication.external_ids == IDS


async def test_locked_reconciliation_refuses_conflicting_ids(monkeypatch, disposable_draft):
    engine, draft_id = disposable_draft
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="failed",
            external_ids={**IDS, "ad_id": "different-ad"},
        ),
    )
    internal = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", internal)

    with pytest.raises(ValueError, match="supplied ad_id .* conflicts"):
        await reconcile_draft(
            engine=engine,
            draft_id=draft_id,
            supplied_ids=IDS,
            settings=object(),
            notifier=object(),
            dry_run=False,
        )

    internal.assert_not_awaited()


async def test_reconciliation_refuses_non_approved_draft_without_writing(
    monkeypatch, disposable_draft
):
    engine, draft_id = disposable_draft
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'rejected' WHERE id = :draft_id"),
            {"draft_id": draft_id},
        )
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", AsyncMock())

    with pytest.raises(ValueError, match="has status 'rejected'"):
        await reconcile_draft(
            engine=engine,
            draft_id=draft_id,
            supplied_ids=IDS,
            settings=object(),
            notifier=object(),
            dry_run=False,
        )

    assert await get_meta_publication(engine, draft_id) is None


async def test_published_draft_with_incomplete_ids_is_not_downgraded(monkeypatch, disposable_draft):
    engine, draft_id = disposable_draft
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :draft_id"),
            {"draft_id": draft_id},
        )
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", AsyncMock())

    with pytest.raises(ValueError, match="already published .* IDs are incomplete"):
        await reconcile_draft(
            engine=engine,
            draft_id=draft_id,
            supplied_ids=IDS,
            settings=object(),
            notifier=object(),
            dry_run=False,
        )

    assert await get_meta_publication(engine, draft_id) is None
