"""Unit tests for durable Meta publication persistence."""

import os
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.publications import (
    MetaPublication,
    get_meta_publication,
    mark_meta_publication_active,
    upsert_meta_publication,
)


class _Result:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row


class _Connection:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _Result(self.row)


class _Engine:
    def __init__(self, row=None):
        self.connection = _Connection(row)

    @asynccontextmanager
    async def begin(self):
        yield self.connection

    @asynccontextmanager
    async def connect(self):
        yield self.connection


@pytest.fixture
async def database_engine():
    engine = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await run_migrations(engine)
    async with engine.begin() as connection:
        action_type_id = (
            await connection.execute(
                text(
                    "INSERT INTO action_types (name, risk_tier, default_autonomy) "
                    "VALUES ('meta-ad', 'high', 'propose') RETURNING id"
                )
            )
        ).scalar_one()
        draft_id = (
            await connection.execute(
                text(
                    "INSERT INTO drafts (action_type_id, channel, language) "
                    "VALUES (:action_type_id, 'meta', 'EN') RETURNING id"
                ),
                {"action_type_id": action_type_id},
            )
        ).scalar_one()
    yield engine, draft_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_preserves_previously_stored_meta_ids(database_engine):
    engine, draft_id = database_engine
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="creating",
            external_ids={
                "campaign_id": "campaign-1",
                "ad_set_id": "ad-set-2",
                "creative_id": "creative-3",
            },
            external_statuses={},
            approved_budget_cents=500,
        ),
    )

    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="created",
            external_ids={"ad_id": "ad-4"},
            external_statuses={},
        ),
    )

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == {
        "campaign_id": "campaign-1",
        "ad_set_id": "ad-set-2",
        "creative_id": "creative-3",
        "ad_id": "ad-4",
    }
    assert stored.approved_budget_cents == 500


@pytest.mark.asyncio
async def test_upsert_populates_json_fields_on_legacy_publication(database_engine):
    engine, draft_id = database_engine
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO publications (draft_id, channel) "
                "VALUES (:draft_id, 'meta')"
            ),
            {"draft_id": draft_id},
        )

    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="creating",
            external_ids={"campaign_id": "campaign-1"},
            external_statuses={"campaign": {"configured_status": "PAUSED"}},
        ),
    )

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == {"campaign_id": "campaign-1"}
    assert stored.external_statuses == {
        "campaign": {"configured_status": "PAUSED"}
    }


@pytest.mark.asyncio
async def test_get_maps_legacy_external_id_to_ad_id(database_engine):
    """The former external_id was the published ad object, not its ancestors."""
    engine, draft_id = database_engine
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO publications (draft_id, channel, external_id) "
                "VALUES (:draft_id, 'meta', 'legacy-ad-4')"
            ),
            {"draft_id": draft_id},
        )

    stored = await get_meta_publication(engine, draft_id)

    assert stored is not None
    assert stored.external_ids == {"ad_id": "legacy-ad-4"}


@pytest.mark.asyncio
async def test_upsert_merges_external_ids_instead_of_replacing_them():
    engine = _Engine()
    publication = MetaPublication(
        draft_id=156,
        state="creating",
        external_ids={"ad_id": "ad-4"},
        external_statuses={},
        failure=None,
        approved_budget_cents=500,
        ads_manager_url=None,
    )

    await upsert_meta_publication(engine, publication)

    sql, params = engine.connection.calls[0]
    assert "external_ids = COALESCE(publications.external_ids, '{}'::JSONB)" in sql
    assert "|| EXCLUDED.external_ids" in sql
    assert params["external_ids"] == '{"ad_id": "ad-4"}'


@pytest.mark.asyncio
async def test_get_meta_publication_returns_typed_record():
    engine = _Engine(
        {
            "draft_id": 156,
            "state": "created",
            "external_ids": {"campaign_id": "campaign-1"},
            "external_statuses": {},
            "failure": None,
            "approved_budget_cents": 500,
            "ads_manager_url": None,
            "created_at": None,
            "updated_at": None,
        }
    )

    publication = await get_meta_publication(engine, 156)

    assert publication == MetaPublication(
        draft_id=156,
        state="created",
        external_ids={"campaign_id": "campaign-1"},
        external_statuses={},
        failure=None,
        approved_budget_cents=500,
        ads_manager_url=None,
    )


@pytest.mark.asyncio
async def test_mark_active_persists_statuses_and_clears_failure():
    engine = _Engine()
    statuses = {"campaign": {"configured_status": "ACTIVE"}}

    await mark_meta_publication_active(engine, 156, statuses)

    sql, params = engine.connection.calls[0]
    assert "state = 'active'" in sql
    assert "failure = NULL" in sql
    assert params["draft_id"] == 156
    assert params["external_statuses"] == '{"campaign": {"configured_status": "ACTIVE"}}'
