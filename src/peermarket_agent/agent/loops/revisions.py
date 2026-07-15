"""Claim, generate, quality-gate, and enqueue Slack draft revisions."""

import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.drafts import Draft
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.revision_generator import revise_draft
from peermarket_agent.revisions import (
    RevisionConflictError,
    claim_feedback_batch,
    list_ready_feedback_threads,
    load_latest_revision_source,
    mark_feedback_failed,
    persist_revision_and_supersede,
    renew_generation_lease,
    requeue_feedback_batch,
)
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


async def _renew_while_generating(engine: AsyncEngine, batch) -> None:
    while True:
        await asyncio.sleep(100)
        if not await renew_generation_lease(
            engine, batch.root_draft_id, batch.lease_owner, lease_seconds=300
        ):
            return


async def run_pending_revisions(
    engine: AsyncEngine, claude: ClaudeClient, notifier: SlackNotifier
) -> int:
    """Process each currently debounce-ready thread once; return variants enqueued."""
    completed = 0
    for channel_id, root_ts in await list_ready_feedback_threads(engine):
        batch = await claim_feedback_batch(engine, channel_id, root_ts)
        if batch is None:
            continue
        heartbeat = asyncio.create_task(_renew_while_generating(engine, batch))
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
            await persist_revision_and_supersede(
                engine,
                predecessor_id,
                accepted,
                batch.feedback_ids,
                outbox_change_summary=generated.change_summary,
                outbox_idempotency_key=f"revision-feedback:{batch.feedback_ids[0]}",
                lease_owner=batch.lease_owner,
            )
            completed += 1
        except asyncio.CancelledError:
            await requeue_feedback_batch(engine, batch.feedback_ids, batch.lease_owner)
            raise
        except RevisionConflictError:
            await requeue_feedback_batch(engine, batch.feedback_ids, batch.lease_owner)
            log.info("revision.conflict_requeued", root_draft_id=batch.root_draft_id)
        except Exception as error:
            category = (
                str(error) if str(error) == "brand_score_below_threshold" else type(error).__name__
            )
            await mark_feedback_failed(
                engine,
                batch.feedback_ids,
                category,
                lease_owner=batch.lease_owner,
            )
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
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
    return completed
