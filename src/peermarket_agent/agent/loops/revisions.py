"""Claim, generate, quality-gate, and enqueue Slack draft revisions."""

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.drafts import Draft
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.revision_generator import revise_draft
from peermarket_agent.revisions import (
    claim_feedback_batch,
    list_ready_feedback_threads,
    load_latest_revision_source,
    mark_feedback_failed,
    persist_revision_and_supersede,
)
from peermarket_agent.slack_dm import format_revised_draft_dm
from peermarket_agent.slack_notifier import SlackNotifier
from peermarket_agent.slack_outbox import enqueue_thread_approval

log = structlog.get_logger(__name__)


async def _fetch_revised_row(engine: AsyncEngine, draft_id: int) -> dict:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT d.id, at.name AS action_type_name, d.language, d.channel, "
                        "d.brand_score, d.copy, d.revision_number, d.revision_feedback "
                        "FROM drafts d JOIN action_types at ON at.id=d.action_type_id WHERE d.id=:id"
                    ),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def run_pending_revisions(
    engine: AsyncEngine, claude: ClaudeClient, notifier: SlackNotifier
) -> int:
    """Process each currently debounce-ready thread once; return variants enqueued."""
    completed = 0
    for channel_id, root_ts in await list_ready_feedback_threads(engine):
        batch = await claim_feedback_batch(engine, channel_id, root_ts)
        if batch is None:
            continue
        persisted = False
        try:
            predecessor_id, source = await load_latest_revision_source(engine, batch.root_draft_id)
            generated = await revise_draft(claude, source, batch.instructions)
            score, _ = await score_draft(
                claude=claude, brand_voice_md=load_brand_voice(), copy=generated.draft.copy
            )
            if score < BRAND_SCORE_THRESHOLD:
                raise ValueError("brand_score_below_threshold")
            accepted = Draft(
                action_type_name=generated.draft.action_type_name,
                channel=generated.draft.channel,
                language=generated.draft.language,
                copy=generated.draft.copy,
                asset_path=generated.draft.asset_path,
                generation_cost_cents=generated.draft.generation_cost_cents,
                brand_score=score,
                visual_truthfulness_pass=generated.draft.visual_truthfulness_pass,
                metadata=generated.draft.metadata,
            )
            draft_id = await persist_revision_and_supersede(
                engine, predecessor_id, accepted, batch.feedback_ids
            )
            persisted = True
            row = await _fetch_revised_row(engine, draft_id)
            message = format_revised_draft_dm(row, change_summary=generated.change_summary)
            await enqueue_thread_approval(engine, draft_id=draft_id, text=message)
            completed += 1
        except Exception as error:
            if persisted:
                log.exception(
                    "revision.approval_enqueue_failed",
                    root_draft_id=batch.root_draft_id,
                    draft_id=draft_id,
                )
                continue
            category = (
                str(error) if str(error) == "brand_score_below_threshold" else type(error).__name__
            )
            await mark_feedback_failed(engine, batch.feedback_ids, category)
            log.warning(
                "revision.generation_failed",
                root_draft_id=batch.root_draft_id,
                failure_category=category,
            )
            try:
                await notifier.send_message(
                    "I couldn't produce a valid revision from that feedback. The current draft remains unchanged.",
                    channel_id=channel_id,
                    thread_ts=root_ts,
                )
            except Exception:
                log.warning("revision.failure_notice_failed", root_draft_id=batch.root_draft_id)
    return completed
