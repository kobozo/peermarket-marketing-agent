"""Unit tests for durable Meta publication persistence."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.meta_insights import MetaInsightSnapshot
from peermarket_agent.performance import derive_performance
from peermarket_agent.publications import (
    MetaPublication,
    MetaReplacementHistoryError,
    begin_meta_terminal_replacement,
    get_meta_publication,
    mark_meta_publication_active,
    record_meta_replacement_result,
    save_performance_snapshot,
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
            text("INSERT INTO publications (draft_id, channel) VALUES (:draft_id, 'meta')"),
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
    assert stored.external_statuses == {"campaign": {"configured_status": "PAUSED"}}


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


async def test_terminal_replacement_atomically_archives_and_clears_current_ids(database_engine):
    engine, draft_id = database_engine
    ids = {"campaign_id": "c-old", "ad_set_id": "s-old", "creative_id": "cr-old", "ad_id": "a-old"}
    statuses = {
        "campaign": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
        "ad_set": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
        "ad": {"status": "DELETED", "effective_status": "DELETED"},
    }
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )

    attempt_id = await begin_meta_terminal_replacement(engine, draft_id, ids, statuses)

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == {}
    assert stored.approved_budget_cents == 1000
    assert len(stored.replacement_history) == 1
    assert {
        key: stored.replacement_history[0][key]
        for key in ("attempt_id", "old_ids", "terminal_statuses", "replacement_ids", "state")
    } == {
        "attempt_id": attempt_id,
        "old_ids": ids,
        "terminal_statuses": statuses,
        "replacement_ids": {},
        "state": "creating",
    }
    assert stored.replacement_history[0]["started_at"]


async def test_terminal_replacement_requires_exact_current_ids_without_write(database_engine):
    engine, draft_id = database_engine
    ids = {"campaign_id": "c-old", "ad_set_id": "s-old", "creative_id": "cr-old", "ad_id": "a-old"}
    await upsert_meta_publication(
        engine, MetaPublication(draft_id=draft_id, state="failed", external_ids=ids)
    )

    with pytest.raises(ValueError, match="stored Meta IDs changed"):
        await begin_meta_terminal_replacement(engine, draft_id, {**ids, "ad_id": "wrong"}, {})

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == ids
    assert stored.replacement_history == []


async def test_replacement_finalizer_rejects_mismatched_attempt(database_engine):
    engine, draft_id = database_engine
    ids = {"campaign_id": "c", "ad_set_id": "s", "creative_id": "cr", "ad_id": "a"}
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id, external_ids=ids))
    await begin_meta_terminal_replacement(engine, draft_id, ids, {})

    with pytest.raises(MetaReplacementHistoryError, match="attempt was not found"):
        await record_meta_replacement_result(
            engine, draft_id, "wrong-attempt", state="failed", failure={"phase": "test"}
        )


async def test_replacement_finalizer_rejects_missing_publication(database_engine):
    engine, draft_id = database_engine

    with pytest.raises(MetaReplacementHistoryError, match="attempt was not found"):
        await record_meta_replacement_result(
            engine, draft_id + 999, "missing", state="failed", failure={"phase": "test"}
        )


async def test_replacement_finalizer_updates_exactly_one_unfinished_attempt(database_engine):
    engine, draft_id = database_engine
    ids = {"campaign_id": "c", "ad_set_id": "s", "creative_id": "cr", "ad_id": "a"}
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id, external_ids=ids))
    attempt_id = await begin_meta_terminal_replacement(engine, draft_id, ids, {})
    await upsert_meta_publication(
        engine, MetaPublication(draft_id=draft_id, external_ids={"campaign_id": "new-c"})
    )

    await record_meta_replacement_result(
        engine, draft_id, attempt_id, state="failed", failure={"phase": "create"}
    )

    stored = await get_meta_publication(engine, draft_id)
    attempt = stored.replacement_history[0]
    assert attempt["replacement_ids"] == {"campaign_id": "new-c"}
    assert attempt["state"] == "failed"
    assert attempt["failure"] == {"phase": "create"}
    assert attempt["finished_at"]


async def test_replacement_finalizer_rejects_duplicate_matching_attempts_without_write(
    database_engine,
):
    engine, draft_id = database_engine
    duplicate = {
        "attempt_id": "duplicate",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "state": "creating",
    }
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE publications SET replacement_history = CAST(:history AS JSONB) "
                "WHERE draft_id = :draft_id"
            ),
            {"draft_id": draft_id, "history": json.dumps([duplicate, duplicate])},
        )
    before = (await get_meta_publication(engine, draft_id)).replacement_history

    with pytest.raises(MetaReplacementHistoryError):
        await record_meta_replacement_result(
            engine, draft_id, "duplicate", state="failed", failure={"phase": "test"}
        )

    assert (await get_meta_publication(engine, draft_id)).replacement_history == before


async def test_replacement_finalizer_rejects_repeated_finalization_without_write(database_engine):
    engine, draft_id = database_engine
    ids = {"campaign_id": "c", "ad_set_id": "s", "creative_id": "cr", "ad_id": "a"}
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id, external_ids=ids))
    attempt_id = await begin_meta_terminal_replacement(engine, draft_id, ids, {})
    await record_meta_replacement_result(
        engine, draft_id, attempt_id, state="failed", failure={"phase": "first"}
    )
    before = (await get_meta_publication(engine, draft_id)).replacement_history

    with pytest.raises(MetaReplacementHistoryError):
        await record_meta_replacement_result(
            engine, draft_id, attempt_id, state="active", failure=None
        )

    assert (await get_meta_publication(engine, draft_id)).replacement_history == before


async def test_save_performance_snapshot_rejects_absent_publication(database_engine):
    engine, draft_id = database_engine

    with pytest.raises(ValueError, match="publication not found"):
        await save_performance_snapshot(engine, draft_id, {"meta": {"impressions": 1}})


async def test_save_performance_snapshot_locks_row_and_merges_namespaces():
    engine = _Engine({"performance": {"attribution": {"registrations": 2}}})

    await save_performance_snapshot(engine, 156, {"meta": {"impressions": 10}})

    select_sql, select_params = engine.connection.calls[0]
    update_sql, update_params = engine.connection.calls[1]
    assert "SELECT performance FROM publications" in select_sql
    assert "FOR UPDATE" in select_sql
    assert select_params == {"draft_id": 156}
    assert "UPDATE publications SET performance" in update_sql
    assert json.loads(update_params["performance"]) == {
        "attribution": {"registrations": 2},
        "meta": {"impressions": 10},
    }


async def test_save_performance_snapshot_retains_fields_in_partially_updated_namespace():
    engine = _Engine(
        {"performance": {"alert_state": {"condition": "no_delivery", "sent_at": "earlier"}}}
    )

    await save_performance_snapshot(engine, 156, {"alert_state": {"condition": "healthy"}})

    _, update_params = engine.connection.calls[1]
    assert json.loads(update_params["performance"])["alert_state"] == {
        "condition": "healthy",
        "sent_at": "earlier",
    }


async def test_save_performance_snapshot_deep_merges_nested_performance_fields(database_engine):
    engine, draft_id = database_engine
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id))
    await save_performance_snapshot(
        engine,
        draft_id,
        {
            "meta": {
                "latest": {"impressions": 10, "clicks": 2},
                "window": {"start": "2026-07-14", "stop": "2026-07-16"},
            }
        },
    )

    await save_performance_snapshot(
        engine,
        draft_id,
        {"meta": {"latest": {"impressions": 11}}},
    )

    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                text("SELECT performance FROM publications WHERE draft_id = :draft_id"),
                {"draft_id": draft_id},
            )
        ).scalar_one()
    assert stored["meta"] == {
        "latest": {"impressions": 11, "clicks": 2},
        "window": {"start": "2026-07-14", "stop": "2026-07-16"},
    }


async def test_save_performance_snapshot_normalizes_real_meta_snapshot_payload(database_engine):
    engine, draft_id = database_engine
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id))
    snapshot = MetaInsightSnapshot(
        ad_id="ad-1",
        window_start=date(2026, 7, 14),
        window_stop=date(2026, 7, 16),
        retrieved_at=datetime(2026, 7, 16, 14, 30, tzinfo=UTC),
        spend_cents=217,
        impressions=125,
        reach=100,
        clicks=10,
        inline_link_clicks=6,
        outbound_clicks=4,
        landing_page_views=3,
        ctr=Decimal("8.00"),
        cpc_cents=22,
        cpm_cents=1736,
        frequency=Decimal("1.250"),
        actions=MappingProxyType({"landing_page_view": 3}),
    )
    payload = {"meta": derive_performance(None, vars(snapshot))}

    await save_performance_snapshot(engine, draft_id, payload)

    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                text("SELECT performance FROM publications WHERE draft_id = :draft_id"),
                {"draft_id": draft_id},
            )
        ).scalar_one()
    latest = stored["meta"]["latest"]
    assert latest["window_start"] == "2026-07-14"
    assert latest["retrieved_at"] == "2026-07-16T14:30:00+00:00"
    assert latest["ctr"] == "8.00"
    assert latest["frequency"] == "1.250"
    assert latest["actions"] == {"landing_page_view": 3}


async def test_save_performance_snapshot_rejects_unsupported_payload_type():
    engine = _Engine({"performance": {}})

    with pytest.raises(TypeError, match="unsupported performance value"):
        await save_performance_snapshot(engine, 156, {"meta": {"invalid": object()}})


async def test_save_performance_snapshot_normalizes_nested_lists_and_tuples():
    engine = _Engine({"performance": {}})

    await save_performance_snapshot(
        engine,
        156,
        {"observations": [("first", Decimal("1.20")), [date(2026, 7, 16)]]},
    )

    _, update_params = engine.connection.calls[1]
    assert json.loads(update_params["performance"])["observations"] == [
        ["first", "1.20"],
        ["2026-07-16"],
    ]


async def test_concurrent_performance_writes_preserve_other_namespaces(database_engine):
    engine, draft_id = database_engine
    await upsert_meta_publication(engine, MetaPublication(draft_id=draft_id))

    await asyncio.gather(
        save_performance_snapshot(engine, draft_id, {"meta": {"impressions": 10}}),
        save_performance_snapshot(engine, draft_id, {"attribution": {"registrations": 2}}),
    )

    async with engine.connect() as connection:
        performance = (
            await connection.execute(
                text("SELECT performance FROM publications WHERE draft_id = :draft_id"),
                {"draft_id": draft_id},
            )
        ).scalar_one()
    assert performance == {
        "meta": {"impressions": 10},
        "attribution": {"registrations": 2},
    }
