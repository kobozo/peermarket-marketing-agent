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
    """CREATE TABLE IF NOT EXISTS video_assets (
        id BIGSERIAL PRIMARY KEY,
        draft_id BIGINT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
        slack_file_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        message_ts TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('source','combined')),
        mime_type TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        duration_seconds DOUBLE PRECISION,
        width INT,
        height INT,
        status TEXT NOT NULL,
        review JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (draft_id, slack_file_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_video_assets_draft_created ON video_assets (draft_id, created_at)",
    "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS message_ts TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS parent_draft_id BIGINT REFERENCES drafts(id)",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS root_draft_id BIGINT REFERENCES drafts(id)",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS revision_number INT NOT NULL DEFAULT 0",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS revision_feedback TEXT",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS revision_feedback_ts TEXT",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS slack_channel_id TEXT",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS slack_root_ts TEXT",
    """DO $$
       DECLARE constraint_name TEXT;
       BEGIN
         FOR constraint_name IN
           SELECT conname FROM pg_constraint
           WHERE conrelid = 'drafts'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%status%'
         LOOP
           EXECUTE format('ALTER TABLE drafts DROP CONSTRAINT %I', constraint_name);
         END LOOP;
         ALTER TABLE drafts ADD CONSTRAINT drafts_status_check
           CHECK (status IN ('queued','approved','rejected','killed','published','superseded'));
       END $$""",
    """CREATE TABLE IF NOT EXISTS draft_revision_feedback (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        channel_id TEXT NOT NULL,
        root_ts TEXT NOT NULL,
        message_ts TEXT NOT NULL,
        feedback_text TEXT NOT NULL,
        root_draft_id BIGINT NOT NULL REFERENCES drafts(id),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','processing','applied','failed')),
        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        claimed_at TIMESTAMPTZ,
        applied_at TIMESTAMPTZ,
        failure_category TEXT,
        UNIQUE (channel_id, root_ts, message_ts)
    )""",
    """CREATE TABLE IF NOT EXISTS slack_outbox (
        id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        draft_id BIGINT NOT NULL REFERENCES drafts(id),
        channel_id TEXT,
        root_ts TEXT,
        message_kind TEXT NOT NULL CHECK (message_kind IN ('root_approval','thread_approval','autonomy_audit')),
        payload JSONB NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','delivered','failed')),
        attempt_count INT NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ,
        last_failure_category TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_slack_root_binding_unique "
    "ON drafts (slack_channel_id, slack_root_ts) WHERE revision_number = 0 "
    "AND slack_channel_id IS NOT NULL AND slack_root_ts IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_root_revision_unique "
    "ON drafts (root_draft_id, revision_number) WHERE root_draft_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_revision_feedback_pending "
    "ON draft_revision_feedback (root_draft_id, status, message_ts)",
    "ALTER TABLE draft_revision_feedback ADD COLUMN IF NOT EXISTS processing_owner TEXT",
    "ALTER TABLE draft_revision_feedback ADD COLUMN IF NOT EXISTS processing_lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE draft_revision_feedback ADD COLUMN IF NOT EXISTS processing_attempts INT NOT NULL DEFAULT 0",
    "ALTER TABLE draft_revision_feedback ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    """CREATE TABLE IF NOT EXISTS draft_revision_generation_leases (
        root_draft_id BIGINT PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
        lease_owner TEXT NOT NULL,
        lease_expires_at TIMESTAMPTZ NOT NULL,
        attempt_count INT NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_slack_outbox_pending ON slack_outbox (status, next_attempt_at)",
    "ALTER TABLE slack_outbox ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE slack_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    """DO $$
       DECLARE constraint_name TEXT;
       BEGIN
         FOR constraint_name IN
           SELECT conname FROM pg_constraint
           WHERE conrelid = 'slack_outbox'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%status%'
         LOOP
           EXECUTE format('ALTER TABLE slack_outbox DROP CONSTRAINT %I', constraint_name);
         END LOOP;
         ALTER TABLE slack_outbox ADD CONSTRAINT slack_outbox_status_check
           CHECK (status IN ('pending','delivered','failed','obsolete'));
       END $$""",
    """DO $$
       DECLARE constraint_name TEXT;
       BEGIN
         FOR constraint_name IN
           SELECT conname FROM pg_constraint
           WHERE conrelid = 'slack_outbox'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%message_kind%'
         LOOP
           EXECUTE format('ALTER TABLE slack_outbox DROP CONSTRAINT %I', constraint_name);
         END LOOP;
         ALTER TABLE slack_outbox ADD CONSTRAINT slack_outbox_message_kind_check
           CHECK (message_kind IN ('root_approval','thread_approval','autonomy_audit'));
       END $$""",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS state TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS external_ids JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS external_statuses JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS failure JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS approved_budget_cents INT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS ads_manager_url TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS replacement_history JSONB NOT NULL DEFAULT '[]'::JSONB",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    "ALTER TABLE publications ALTER COLUMN performance SET DEFAULT '{}'::JSONB",
    "UPDATE publications SET performance = '{}'::JSONB WHERE performance IS NULL",
    "ALTER TABLE publications ALTER COLUMN performance SET NOT NULL",
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
    """CREATE TABLE IF NOT EXISTS operational_alert_state (
        alert_key TEXT PRIMARY KEY,
        state JSONB NOT NULL DEFAULT '{}'::JSONB,
        claim JSONB NOT NULL DEFAULT '{}'::JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_kpis_hourly_metric ON kpis_hourly (metric_name, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_slack_actions_status ON slack_actions (status)",
    """CREATE TABLE IF NOT EXISTS daily_performance_summary_outbox (
        id BIGSERIAL PRIMARY KEY,
        summary_key TEXT NOT NULL UNIQUE,
        window_start DATE NOT NULL,
        window_stop DATE NOT NULL,
        window_definition TEXT NOT NULL,
        publication_ids JSONB NOT NULL,
        evidence_ids JSONB NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','sent')),
        attempt_count INT NOT NULL DEFAULT 0,
        last_attempt_at TIMESTAMPTZ,
        sent_at TIMESTAMPTZ,
        claim_token TEXT,
        claim_expires_at TIMESTAMPTZ,
        last_failure TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_daily_performance_summary_pending "
    "ON daily_performance_summary_outbox (status, window_start, id)",
    "ALTER TABLE daily_performance_summary_outbox "
    "ADD COLUMN IF NOT EXISTS summary_kind TEXT NOT NULL DEFAULT 'evidence_summary'",
    "ALTER TABLE daily_performance_summary_outbox ADD COLUMN IF NOT EXISTS run_day DATE",
    "ALTER TABLE daily_performance_summary_outbox ALTER COLUMN window_start DROP NOT NULL",
    "ALTER TABLE daily_performance_summary_outbox ALTER COLUMN window_stop DROP NOT NULL",
    "ALTER TABLE daily_performance_summary_outbox ALTER COLUMN window_definition DROP NOT NULL",
    """CREATE TABLE IF NOT EXISTS autonomous_decisions (
        id BIGSERIAL PRIMARY KEY,
        decision_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('observe','pause','replace','reallocate','scale')),
        campaign_id TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        evidence JSONB NOT NULL,
        reason TEXT NOT NULL,
        old_budget_cents INT,
        new_budget_cents INT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE OR REPLACE FUNCTION reject_autonomous_decision_mutation()
       RETURNS TRIGGER AS $$
       BEGIN
         RAISE EXCEPTION 'autonomous_decisions is append-only';
       END;
       $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS autonomous_decisions_append_only ON autonomous_decisions",
    """CREATE TRIGGER autonomous_decisions_append_only
       BEFORE UPDATE OR DELETE ON autonomous_decisions
       FOR EACH ROW EXECUTE FUNCTION reject_autonomous_decision_mutation()""",
    """CREATE TABLE IF NOT EXISTS autonomous_actions (
        id BIGSERIAL PRIMARY KEY,
        decision_id BIGINT NOT NULL REFERENCES autonomous_decisions(id),
        campaign_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','leased','executing','succeeded','failed','cancelled',
                              'reconciliation_required')),
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        before_state JSONB NOT NULL DEFAULT '{}'::JSONB,
        after_state JSONB NOT NULL DEFAULT '{}'::JSONB,
        audit JSONB NOT NULL DEFAULT '{}'::JSONB,
        failure_category TEXT,
        failure_message TEXT,
        next_evaluation_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomous_actions_campaign_nonterminal "
    "ON autonomous_actions (campaign_id) "
    "WHERE status IN ('pending','leased','executing')",
    "CREATE INDEX IF NOT EXISTS idx_autonomous_actions_claimable "
    "ON autonomous_actions (status, lease_expires_at, id)",
    """CREATE TABLE IF NOT EXISTS autonomous_budget_events (
        id BIGSERIAL PRIMARY KEY,
        action_id BIGINT NOT NULL REFERENCES autonomous_actions(id),
        campaign_id TEXT NOT NULL,
        old_budget_cents INT NOT NULL CHECK (old_budget_cents > 0),
        new_budget_cents INT NOT NULL CHECK (new_budget_cents > 0),
        amount_cents INT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_autonomous_budget_events_campaign_created "
    "ON autonomous_budget_events (campaign_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS autonomous_replacement_publications (
        id BIGSERIAL PRIMARY KEY,
        action_id BIGINT NOT NULL REFERENCES autonomous_actions(id),
        replacement_draft_id BIGINT NOT NULL REFERENCES drafts(id),
        source_draft_id BIGINT NOT NULL REFERENCES drafts(id),
        state TEXT NOT NULL DEFAULT 'creating'
            CHECK (state IN ('creating','paused','reconciliation_required')),
        frozen_budget_cents INT NOT NULL CHECK (frozen_budget_cents > 0),
        source_campaign_id TEXT NOT NULL,
        changed_dimension TEXT NOT NULL,
        landing_page_url TEXT NOT NULL,
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        progress JSONB NOT NULL DEFAULT '{}'::JSONB,
        failure_category TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (action_id)
    )""",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS source_campaign_id TEXT",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS changed_dimension TEXT",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS landing_page_url TEXT",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS lease_token TEXT",
    "ALTER TABLE autonomous_replacement_publications ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomous_replacement_one_per_action ON autonomous_replacement_publications(action_id)",
    """CREATE TABLE IF NOT EXISTS autonomous_replacement_generations (
        action_id BIGINT PRIMARY KEY REFERENCES autonomous_actions(id),
        state TEXT NOT NULL DEFAULT 'generating'
            CHECK (state IN ('generating','completed')),
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        replacement_draft_id BIGINT UNIQUE REFERENCES drafts(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS autonomous_action_id BIGINT REFERENCES autonomous_actions(id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_one_autonomous_action ON drafts(autonomous_action_id) WHERE autonomous_action_id IS NOT NULL",
    """CREATE OR REPLACE FUNCTION reject_autonomous_budget_event_mutation()
       RETURNS TRIGGER AS $$
       BEGIN
         RAISE EXCEPTION 'autonomous_budget_events is append-only';
       END;
       $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS autonomous_budget_events_append_only ON autonomous_budget_events",
    """CREATE TRIGGER autonomous_budget_events_append_only
       BEFORE UPDATE OR DELETE ON autonomous_budget_events
       FOR EACH ROW EXECUTE FUNCTION reject_autonomous_budget_event_mutation()""",
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
