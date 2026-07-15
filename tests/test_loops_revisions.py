import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.revisions import run_pending_revisions
from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.revisions import (
    RevisionConflictError,
    RevisionFeedbackEvent,
    bind_draft_thread,
    record_revision_feedback,
    retry_failed_feedback,
)


@pytest.fixture
async def engine():
    eng = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    yield eng
    await eng.dispose()


async def setup_feedback(engine, text_value="Shorter"):
    original = await persist_draft(
        engine,
        Draft(
            "tiktok_post_organic",
            "tiktok",
            "NL",
            "Old\n\nBody\n\nCTA",
            None,
            1,
            90,
            True,
            {"fact": "keep"},
        ),
    )
    await bind_draft_thread(engine, original, "D1", "100.0")
    await record_revision_feedback(
        engine, RevisionFeedbackEvent("Ev1", "D1", "100.0", "100.1", text_value)
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE draft_revision_feedback SET received_at=NOW()-INTERVAL '16 seconds'")
        )
    return original


def generation(text=None):
    return ClaudeResponse(
        text=text
        or '{"hook":"Wil je vandaag veilig en lokaal spullen verkopen?","body":"Veilig.","cta":"Plaats het nu","change_summary":"Korter"}',
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
    )


def score(value=90):
    return ClaudeResponse(
        text=f'{{"score":{value},"notes":"ok"}}',
        input_tokens=50,
        output_tokens=10,
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
    )


async def test_valid_revision_supersedes_and_enqueues_same_thread_approval(engine):
    original = await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score()]

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status, revision_number, brand_score FROM drafts ORDER BY id")
            )
        ).fetchall()
        outbox = (
            await conn.execute(
                text("SELECT channel_id, root_ts, message_kind, payload->>'text' FROM slack_outbox")
            )
        ).one()
        feedback_status = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()
    assert rows == [("superseded", 0, 90), ("queued", 1, 90)]
    assert outbox[:3] == ("D1", "100.0", "thread_approval")
    assert "Changes applied" in outbox[3] and f"❌ {original + 1}" in outbox[3]
    assert feedback_status == "applied"
    assert outbox[3].count(str(original + 1)) >= 2


@pytest.mark.parametrize(("generated", "brand"), [("not-json", None), (None, 79)])
async def test_invalid_or_below_threshold_marks_feedback_failed_without_superseding(
    engine, generated, brand
):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = (
        [generation(generated)] if brand is None else [generation(), score(brand)]
    )
    notifier = AsyncMock()

    assert await run_pending_revisions(engine, claude, notifier) == 0

    async with engine.connect() as conn:
        draft_status = (await conn.execute(text("SELECT status FROM drafts"))).scalar_one()
        feedback = (
            await conn.execute(text("SELECT status, failure_category FROM draft_revision_feedback"))
        ).one()
        outbox_count = (await conn.execute(text("SELECT count(*) FROM slack_outbox"))).scalar_one()
    assert draft_status == "queued"
    assert feedback[0] == "failed"
    assert feedback[1]
    assert outbox_count == 0
    notifier.send_message.assert_awaited_once()


async def test_feedback_arriving_during_generation_stays_pending_for_next_batch(engine):
    await setup_feedback(engine, "First")
    claude = AsyncMock()

    async def complete(**kwargs):
        if "<founder_feedback_data>" in kwargs.get("user", ""):
            await record_revision_feedback(
                engine, RevisionFeedbackEvent("Ev2", "D1", "100.0", "100.2", "Second")
            )
            assert "Second" not in kwargs["user"]
            return generation()
        return score()

    claude.complete.side_effect = complete

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1
    async with engine.connect() as conn:
        statuses = (
            await conn.execute(
                text(
                    "SELECT feedback_text, status FROM draft_revision_feedback ORDER BY message_ts"
                )
            )
        ).fetchall()
    assert statuses == [("First", "applied"), ("Second", "pending")]

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE draft_revision_feedback SET received_at=NOW()-INTERVAL '16 seconds' "
                "WHERE status='pending'"
            )
        )
    second_claude = AsyncMock()

    async def second_complete(**kwargs):
        if "<source_draft_data>" in kwargs.get("user", ""):
            assert "Wil je vandaag veilig en lokaal spullen verkopen?" in kwargs["user"]
            return generation(
                '{"hook":"Wil je nu betrouwbaar en lokaal jouw spullen verkopen?","body":"Veilig.","cta":"Plaats het nu","change_summary":"Tweede"}'
            )
        return score()

    second_claude.complete.side_effect = second_complete
    assert await run_pending_revisions(engine, second_claude, AsyncMock()) == 1
    async with engine.connect() as conn:
        revisions = (
            (await conn.execute(text("SELECT revision_number FROM drafts ORDER BY id")))
            .scalars()
            .all()
        )
    assert revisions == [0, 1, 2]


async def test_repeated_loop_does_not_duplicate_generation(engine):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score()]
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    assert claude.complete.await_count == 2


async def test_cancellation_requeues_owned_batch_and_releases_root(engine):
    await setup_feedback(engine)
    started = asyncio.Event()
    claude = AsyncMock()

    async def blocked(**kwargs):
        started.set()
        await asyncio.Event().wait()

    claude.complete.side_effect = blocked
    task = asyncio.create_task(run_pending_revisions(engine, claude, AsyncMock()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()
        leases = (
            await conn.execute(text("SELECT count(*) FROM draft_revision_generation_leases"))
        ).scalar_one()
    assert status == "pending"
    assert leases == 0


async def test_latest_leaf_conflict_requeues_instead_of_failing(engine, monkeypatch):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score()]
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.revisions.persist_revision_and_supersede",
        AsyncMock(side_effect=RevisionConflictError("latest changed")),
    )

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()
        leases = (
            await conn.execute(text("SELECT count(*) FROM draft_revision_generation_leases"))
        ).scalar_one()
    assert status == "pending"
    assert leases == 0


async def test_transient_generation_failure_retries_then_succeeds(engine):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [TimeoutError("provider timeout")]

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    async with engine.connect() as conn:
        first = (
            await conn.execute(
                text(
                    "SELECT status, processing_attempts, next_attempt_at > NOW() "
                    "FROM draft_revision_feedback"
                )
            )
        ).one()
    assert first == ("pending", 1, True)

    async with engine.begin() as conn:
        await conn.execute(text("UPDATE draft_revision_feedback SET next_attempt_at=NOW()"))
    claude.complete.side_effect = [generation(), score()]
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1


async def test_transient_generation_failure_stops_at_attempt_cap(engine):
    await setup_feedback(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE draft_revision_feedback SET processing_attempts=2"))
    claude = AsyncMock()
    claude.complete.side_effect = TimeoutError("provider timeout")

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, processing_attempts FROM draft_revision_feedback")
            )
        ).one()
    assert row == ("failed", 3)


async def test_permanent_validation_failure_can_be_manually_requeued(engine):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score(79)]
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    async with engine.connect() as conn:
        feedback_id = (
            await conn.execute(text("SELECT id FROM draft_revision_feedback"))
        ).scalar_one()

    assert await retry_failed_feedback(engine, (feedback_id,)) == 1
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT status, failure_category FROM draft_revision_feedback"))
        ).one()
    assert row == ("pending", None)


async def test_heartbeat_database_failure_does_not_undo_committed_revision(engine, monkeypatch):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score()]
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.revisions._renew_while_generating",
        AsyncMock(side_effect=RuntimeError("heartbeat db unavailable")),
    )

    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1
    async with engine.connect() as conn:
        statuses = (
            (await conn.execute(text("SELECT status FROM drafts ORDER BY revision_number")))
            .scalars()
            .all()
        )
    assert statuses == ["superseded", "queued"]
