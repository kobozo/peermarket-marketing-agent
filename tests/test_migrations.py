"""Migration runner tests — idempotency + schema shape."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import _STEPS, run_migrations

REQUIRED_TABLES = {
    "schema_version",
    "brand_voice",
    "action_types",
    "trust_scores",
    "drafts",
    "video_assets",
    "publications",
    "budget_ledger",
    "kpis_hourly",
    "slack_actions",
    "strategy_memos",
    "creatives_archive",
    "self_extensions",
    "learnings",
    "draft_revision_feedback",
    "slack_outbox",
    "operational_alert_state",
    "daily_performance_summary_outbox",
    "autonomous_decisions",
    "autonomous_actions",
    "autonomous_budget_events",
}


def test_autonomy_migration_has_durable_constraints_and_audit_fields():
    migration_sql = "\n".join(_STEPS).lower()

    assert "create table if not exists autonomous_decisions" in migration_sql
    assert "decision_key text not null unique" in migration_sql
    assert "evidence jsonb not null" in migration_sql
    assert "create table if not exists autonomous_actions" in migration_sql
    assert "references autonomous_decisions(id)" in migration_sql
    assert "lease_owner text" in migration_sql
    assert "lease_token text" in migration_sql
    assert "lease_expires_at timestamptz" in migration_sql
    assert "before_state jsonb" in migration_sql
    assert "after_state jsonb" in migration_sql
    assert "audit jsonb" in migration_sql
    assert "check (status in" in migration_sql
    assert (
        "create unique index if not exists idx_autonomous_actions_campaign_nonterminal"
        in migration_sql
    )
    assert "where status in ('pending','leased','executing')" in migration_sql
    assert "create table if not exists autonomous_budget_events" in migration_sql
    assert "amount_cents int not null" in migration_sql
    assert "created_at timestamptz not null default now()" in migration_sql
    assert "autonomous_decisions_append_only" in migration_sql


def test_publications_migration_adds_reconciliation_columns_and_unique_draft_index():
    migration_sql = "\n".join(_STEPS).lower()

    for column in (
        "state",
        "external_ids",
        "external_statuses",
        "failure",
        "approved_budget_cents",
        "ads_manager_url",
        "updated_at",
    ):
        assert f"alter table publications add column if not exists {column}" in migration_sql

    assert "create unique index if not exists" in migration_sql
    assert "on publications (draft_id) where draft_id is not null" in migration_sql


@pytest.fixture
async def engine():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    yield eng
    await eng.dispose()


async def test_migrations_create_all_expected_tables(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        tables = {row[0] for row in result.fetchall()}
    missing = REQUIRED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_migrations_are_idempotent(engine):
    await run_migrations(engine)
    await run_migrations(engine)  # second run must not raise
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM action_types"))
        # Schema-only — seed lives in T4. Count is 0 here.
        assert result.scalar() == 0


async def test_autonomous_decisions_are_append_only_at_database_level(engine):
    await run_migrations(engine)
    async with engine.begin() as conn:
        decision_id = (
            await conn.execute(
                text(
                    "INSERT INTO autonomous_decisions "
                    "(decision_key, kind, campaign_id, window_start, window_end, evidence, reason) "
                    "VALUES ('decision-1', 'observe', '123', NOW() - INTERVAL '1 hour', NOW(), "
                    "'{\"snapshot_id\": 42}'::jsonb, 'observe') RETURNING id"
                )
            )
        ).scalar_one()

    for statement in (
        "UPDATE autonomous_decisions SET reason = 'changed' WHERE id = :decision_id",
        "DELETE FROM autonomous_decisions WHERE id = :decision_id",
    ):
        with pytest.raises(Exception, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"decision_id": decision_id})

    async with engine.connect() as conn:
        reason = await conn.scalar(
            text("SELECT reason FROM autonomous_decisions WHERE id = :decision_id"),
            {"decision_id": decision_id},
        )
    assert reason == "observe"


async def test_revision_schema_has_lineage_bindings_and_superseded_status(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        columns = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'drafts'"
                    )
                )
            ).fetchall()
        }
        constraint = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'drafts'::regclass AND contype = 'c' "
                    "AND pg_get_constraintdef(oid) LIKE '%status%'"
                )
            )
        ).scalar_one()

    assert {
        "parent_draft_id",
        "root_draft_id",
        "revision_number",
        "revision_feedback",
        "revision_feedback_ts",
        "slack_channel_id",
        "slack_root_ts",
    } <= columns
    assert "superseded" in constraint


async def test_revision_schema_enforces_root_feedback_and_outbox_idempotency(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        indexes = {
            row[0]: row[1]
            for row in (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND tablename IN "
                        "('drafts', 'draft_revision_feedback', 'slack_outbox')"
                    )
                )
            ).fetchall()
        }

    assert any(
        "UNIQUE" in definition
        and "slack_channel_id" in definition
        and "slack_root_ts" in definition
        for definition in indexes.values()
    )
    assert any(
        "UNIQUE" in definition and "event_id" in definition for definition in indexes.values()
    )
    assert any(
        "UNIQUE" in definition and "idempotency_key" in definition
        for definition in indexes.values()
    )


async def test_migrations_reconcile_duplicate_draft_publications_before_unique_index(engine):
    unique_index_step = next(step for step in _STEPS if "idx_publications_draft_id_unique" in step)
    async with engine.begin() as conn:
        for step in _STEPS:
            if step == unique_index_step:
                continue
            await conn.execute(text(step))
        action_type_id = (
            await conn.execute(
                text(
                    "INSERT INTO action_types (name, risk_tier, default_autonomy) "
                    "VALUES ('meta-ad', 'high', 'propose') RETURNING id"
                )
            )
        ).scalar_one()
        draft_id = (
            await conn.execute(
                text(
                    "INSERT INTO drafts (action_type_id, channel, language) "
                    "VALUES (:action_type_id, 'meta', 'EN') RETURNING id"
                ),
                {"action_type_id": action_type_id},
            )
        ).scalar_one()
        first_id = (
            await conn.execute(
                text(
                    "INSERT INTO publications "
                    "(draft_id, channel, external_id, external_ids, performance) "
                    "VALUES (:draft_id, 'meta', 'legacy-ad-4', "
                    '\'{"campaign_id": "campaign-1"}\', \'{"clicks": 2}\') '
                    "RETURNING id"
                ),
                {"draft_id": draft_id},
            )
        ).scalar_one()
        second_id = (
            await conn.execute(
                text(
                    "INSERT INTO publications "
                    "(draft_id, channel, state, external_ids, external_statuses, "
                    "approved_budget_cents, ads_manager_url, performance) VALUES "
                    "(:draft_id, 'meta', 'created', "
                    '\'{"ad_set_id": "ad-set-2"}\', '
                    '\'{"campaign": {"configured_status": "PAUSED"}}\', '
                    "500, 'https://ads.example.test/campaign-1', "
                    "'{\"spend_cents\": 25}') RETURNING id"
                ),
                {"draft_id": draft_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO creatives_archive (publication_id, asset_path) "
                "VALUES (:publication_id, '/tmp/ad.png')"
            ),
            {"publication_id": second_id},
        )

    await run_migrations(engine)
    await run_migrations(engine)

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT * FROM publications WHERE draft_id = :draft_id"),
                    {"draft_id": draft_id},
                )
            )
            .mappings()
            .all()
        )
        creative_publication_id = (
            await conn.execute(text("SELECT publication_id FROM creatives_archive"))
        ).scalar_one()
        index_exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname = 'idx_publications_draft_id_unique'"
                )
            )
        ).scalar_one()

    assert len(rows) == 1
    assert rows[0]["id"] == first_id
    assert rows[0]["external_ids"] == {
        "campaign_id": "campaign-1",
        "ad_set_id": "ad-set-2",
        "ad_id": "legacy-ad-4",
    }
    assert rows[0]["external_statuses"] == {"campaign": {"configured_status": "PAUSED"}}
    assert rows[0]["state"] == "created"
    assert rows[0]["approved_budget_cents"] == 500
    assert rows[0]["ads_manager_url"] == "https://ads.example.test/campaign-1"
    assert rows[0]["performance"] == {"clicks": 2, "spend_cents": 25}
    assert creative_publication_id == first_id
    assert index_exists == 1


async def test_pgvector_extension_enabled(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT extname FROM pg_extension WHERE extname='vector'"))
        assert result.scalar() == "vector"


async def test_creatives_archive_has_vector_column(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='creatives_archive' AND column_name='embedding'"
            )
        )
        # pgvector reports as 'USER-DEFINED'
        assert result.scalar() == "USER-DEFINED"


async def test_video_assets_include_slack_message_timestamp(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        timestamp_type = await conn.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'video_assets' AND column_name = 'message_ts'"
            )
        )

    assert timestamp_type == "text"
