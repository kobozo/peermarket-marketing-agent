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
    RevisionFeedbackEvent,
    bind_draft_thread,
    record_revision_feedback,
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
        or '{"hook":"Nieuw?","body":"Veilig.","cta":"Plaats nu","change_summary":"Korter"}',
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


async def test_repeated_loop_does_not_duplicate_generation(engine):
    await setup_feedback(engine)
    claude = AsyncMock()
    claude.complete.side_effect = [generation(), score()]
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 1
    assert await run_pending_revisions(engine, claude, AsyncMock()) == 0
    assert claude.complete.await_count == 2
