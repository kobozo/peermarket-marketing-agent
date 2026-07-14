"""Idempotent SQL migrations. Applied on every service start.

Pattern matches kobozo/secondhand: a list of SQL strings, each safe to
re-run. No state table is maintained — every statement uses IF NOT EXISTS
or its equivalent.
"""

import asyncio

import click
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger(__name__)


_STEPS: list[str] = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS schema_version (
        id INT PRIMARY KEY DEFAULT 1,
        applied_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS brand_voice (
        id INT PRIMARY KEY DEFAULT 1,
        voice_rules_md TEXT NOT NULL DEFAULT '',
        banned_phrases JSONB NOT NULL DEFAULT '[]',
        approved_examples JSONB NOT NULL DEFAULT '[]',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (id = 1)
    )""",
    """CREATE TABLE IF NOT EXISTS action_types (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        risk_tier TEXT NOT NULL CHECK (risk_tier IN ('low','mid','high')),
        default_autonomy TEXT NOT NULL CHECK (default_autonomy IN ('propose','auto-veto','auto-digest'))
    )""",
    """CREATE TABLE IF NOT EXISTS trust_scores (
        action_type_id INT PRIMARY KEY REFERENCES action_types(id),
        window_30d_approval_rate NUMERIC(5,2) NOT NULL DEFAULT 0.0,
        last_graduated_at TIMESTAMPTZ,
        last_demoted_at TIMESTAMPTZ,
        current_mode TEXT NOT NULL DEFAULT 'propose'
            CHECK (current_mode IN ('propose','auto-veto','auto-digest'))
    )""",
    """CREATE TABLE IF NOT EXISTS drafts (
        id BIGSERIAL PRIMARY KEY,
        action_type_id INT NOT NULL REFERENCES action_types(id),
        channel TEXT NOT NULL,
        language TEXT NOT NULL,
        copy TEXT NOT NULL DEFAULT '',
        asset_path TEXT,
        generation_cost_cents INT NOT NULL DEFAULT 0,
        brand_score INT,
        visual_truthfulness_pass BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','approved','rejected','killed','published')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        decided_at TIMESTAMPTZ,
        decided_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS publications (
        id BIGSERIAL PRIMARY KEY,
        draft_id BIGINT REFERENCES drafts(id),
        external_id TEXT,
        channel TEXT NOT NULL,
        published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        performance JSONB NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS budget_ledger (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        channel TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('spend','credit-earned','cap-change')),
        amount_cents INT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        links_to JSONB NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS kpis_hourly (
        ts TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        value NUMERIC NOT NULL,
        dims JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (ts, source, metric_name)
    )""",
    """CREATE TABLE IF NOT EXISTS slack_actions (
        id BIGSERIAL PRIMARY KEY,
        slack_ts TEXT,
        channel TEXT NOT NULL,
        action_type TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','approved','rejected','timed_out','killed')),
        decided_at TIMESTAMPTZ,
        decided_by_emoji TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS strategy_memos (
        id BIGSERIAL PRIMARY KEY,
        week_start_date DATE NOT NULL UNIQUE,
        body_md TEXT NOT NULL,
        hypotheses JSONB NOT NULL DEFAULT '[]',
        approved_in_thread BOOLEAN NOT NULL DEFAULT FALSE,
        human_response_md TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS creatives_archive (
        id BIGSERIAL PRIMARY KEY,
        publication_id BIGINT REFERENCES publications(id),
        asset_path TEXT NOT NULL,
        prompt TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        cost_cents INT NOT NULL DEFAULT 0,
        embedding vector(1536),
        performance_summary JSONB NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS self_extensions (
        id BIGSERIAL PRIMARY KEY,
        pr_url TEXT NOT NULL,
        target_repo TEXT NOT NULL,
        kind TEXT NOT NULL,
        summary_md TEXT NOT NULL DEFAULT '',
        opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        merged_at TIMESTAMPTZ,
        reverted_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS learnings (
        id BIGSERIAL PRIMARY KEY,
        scope TEXT NOT NULL,
        text TEXT NOT NULL,
        evidence_links JSONB NOT NULL DEFAULT '[]',
        confidence INT NOT NULL DEFAULT 50,
        seen_n_times INT NOT NULL DEFAULT 1
    )""",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS state TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS external_ids JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS external_statuses JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS failure JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS approved_budget_cents INT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS ads_manager_url TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS replacement_history JSONB NOT NULL DEFAULT '[]'::JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    """UPDATE publications
       SET external_ids = jsonb_build_object('ad_id', external_id)
           || COALESCE(external_ids, '{}'::JSONB)
       WHERE external_id IS NOT NULL""",
    """UPDATE publications AS keeper
       SET external_id = (
               SELECT source.external_id FROM publications AS source
               WHERE source.draft_id = keeper.draft_id AND source.external_id IS NOT NULL
               ORDER BY source.id DESC LIMIT 1
           ),
           state = (
               SELECT source.state FROM publications AS source
               WHERE source.draft_id = keeper.draft_id AND source.state IS NOT NULL
               ORDER BY source.id DESC LIMIT 1
           ),
           external_ids = COALESCE((
               SELECT jsonb_object_agg(item.key, item.value ORDER BY source.id)
               FROM publications AS source
               CROSS JOIN LATERAL jsonb_each(COALESCE(source.external_ids, '{}'::JSONB)) AS item
               WHERE source.draft_id = keeper.draft_id
           ), '{}'::JSONB),
           external_statuses = COALESCE((
               SELECT jsonb_object_agg(item.key, item.value ORDER BY source.id)
               FROM publications AS source
               CROSS JOIN LATERAL jsonb_each(
                   COALESCE(source.external_statuses, '{}'::JSONB)
               ) AS item
               WHERE source.draft_id = keeper.draft_id
           ), '{}'::JSONB),
           failure = (
               SELECT source.failure FROM publications AS source
               WHERE source.draft_id = keeper.draft_id AND source.failure IS NOT NULL
               ORDER BY source.id DESC LIMIT 1
           ),
           approved_budget_cents = (
               SELECT source.approved_budget_cents FROM publications AS source
               WHERE source.draft_id = keeper.draft_id
                   AND source.approved_budget_cents IS NOT NULL
               ORDER BY source.id DESC LIMIT 1
           ),
           ads_manager_url = (
               SELECT source.ads_manager_url FROM publications AS source
               WHERE source.draft_id = keeper.draft_id AND source.ads_manager_url IS NOT NULL
               ORDER BY source.id DESC LIMIT 1
           ),
           performance = COALESCE((
               SELECT jsonb_object_agg(item.key, item.value ORDER BY source.id)
               FROM publications AS source
               CROSS JOIN LATERAL jsonb_each(COALESCE(source.performance, '{}'::JSONB)) AS item
               WHERE source.draft_id = keeper.draft_id
           ), '{}'::JSONB),
           updated_at = (
               SELECT MAX(source.updated_at) FROM publications AS source
               WHERE source.draft_id = keeper.draft_id
           )
       WHERE keeper.draft_id IS NOT NULL
         AND keeper.id = (
             SELECT MIN(candidate.id) FROM publications AS candidate
             WHERE candidate.draft_id = keeper.draft_id
         )
         AND EXISTS (
             SELECT 1 FROM publications AS duplicate
             WHERE duplicate.draft_id = keeper.draft_id AND duplicate.id <> keeper.id
         )""",
    """UPDATE creatives_archive AS creative
       SET publication_id = survivor.id
       FROM publications AS duplicate
       JOIN publications AS survivor
         ON survivor.id = (
             SELECT MIN(candidate.id) FROM publications AS candidate
             WHERE candidate.draft_id = duplicate.draft_id
         )
       WHERE creative.publication_id = duplicate.id
         AND duplicate.draft_id IS NOT NULL
         AND duplicate.id <> survivor.id""",
    """DELETE FROM publications AS duplicate
       WHERE duplicate.draft_id IS NOT NULL
         AND duplicate.id <> (
             SELECT MIN(survivor.id) FROM publications AS survivor
             WHERE survivor.draft_id = duplicate.draft_id
         )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_publications_draft_id_unique "
    "ON publications (draft_id) WHERE draft_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_kpis_hourly_metric ON kpis_hourly (metric_name, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_slack_actions_status ON slack_actions (status)",
]


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for step in _STEPS:
            await conn.execute(text(step))
        await conn.execute(
            text(
                "INSERT INTO schema_version (id) VALUES (1) "
                "ON CONFLICT (id) DO UPDATE SET applied_at = NOW()"
            )
        )
    log.info("migrations.applied", steps=len(_STEPS))


@click.command()
def cli() -> None:
    """Run migrations against AGENT_DB_URL."""
    from peermarket_agent.db.engine import get_engine

    asyncio.run(run_migrations(get_engine()))


if __name__ == "__main__":
    cli()
