"""Loop B (MVP) — daily proactive draft generation + Slack DM delivery.

Runs once at 09:00 Europe/Brussels. Generates one draft of each of 3
action types (Meta, TikTok NL, email NL). DMs the founder for each
draft that passed the brand-quality gate, plus a summary at the end.

Phase 1b will replace the plain-text DMs with proper Block-Kit approval
cards (buttons, edit modals, variant regeneration). Phase 1c will add
the strategy memo and the action-type rotation logic.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.agent.cli_draft import run_draft_command
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.slack_bridge.video_events import update_draft_thread_metadata
from peermarket_agent.slack_dm import format_draft_dm, format_summary_dm
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)

_TZ = ZoneInfo("Europe/Brussels")


# Today's plan: one Meta ad (random audience), one NL TikTok (declutter),
# one NL email re-engagement (dormant signups). Phase 1c replaces this
# static plan with rotation + strategy-memo-driven selection.
_TODAYS_PLAN: list[dict[str, Any]] = [
    {"action_type_name": "meta_ad_creative", "language": "NL"},
    {
        "action_type_name": "tiktok_post_organic",
        "language": "NL",
        "theme": "declutter",
    },
    {
        "action_type_name": "email_re_engagement",
        "language": "NL",
        "audience": "dormant_signups",
    },
]


async def _seconds_until_next_9am() -> float:
    """Seconds until the next 09:00 Europe/Brussels. Always strictly future."""
    now = datetime.now(_TZ)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def _fetch_draft_with_action_name(engine: AsyncEngine, draft_id: int) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.id, at.name AS action_type_name, d.language, "
                    "d.channel, d.brand_score, d.copy, d.metadata "
                    "FROM drafts d JOIN action_types at ON at.id = d.action_type_id "
                    "WHERE d.id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "action_type_name": row[1],
        "language": row[2],
        "channel": row[3],
        "brand_score": row[4],
        "copy": row[5],
        **(row[6] or {}),
    }


async def _record_loop_b_metric(engine: AsyncEngine, persisted: int) -> None:
    """Stamp a row in kpis_hourly keyed at today's 09:00 Europe/Brussels."""
    now = datetime.now(_TZ).replace(minute=0, second=0, microsecond=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO kpis_hourly (ts, source, metric_name, value) "
                "VALUES (:ts, 'agent-internal', 'daily_drafts_generated', :v) "
                "ON CONFLICT (ts, source, metric_name) DO UPDATE "
                "SET value = EXCLUDED.value"
            ),
            {"ts": now, "v": persisted},
        )


async def run_daily_drafts(
    *,
    engine: AsyncEngine,
    claude: ClaudeClient,
    notifier: SlackNotifier,
) -> int:
    """Run Loop B once. Returns count of drafts that persisted + were DM'd."""
    persisted = 0
    for plan in _TODAYS_PLAN:
        action = plan["action_type_name"]
        kwargs = {k: v for k, v in plan.items() if k != "action_type_name"}
        try:
            draft_id = await run_draft_command(
                engine=engine,
                claude=claude,
                notifier=notifier,
                action_type_name=action,
                **kwargs,
            )
        except Exception:
            log.exception("loop_b.draft_failed", action=action)
            continue
        if draft_id is None:
            log.info("loop_b.draft_rejected_by_gate", action=action)
            continue
        draft = await _fetch_draft_with_action_name(engine, draft_id)
        if draft is None:
            log.warning("loop_b.draft_disappeared", action=action, draft_id=draft_id)
            continue
        message = format_draft_dm(draft)
        if action == "tiktok_post_organic":
            # The root post is the single founder notification for TikTok. Its
            # timestamp is persisted so uploads can be authorized in this thread.
            try:
                reference = await notifier.post_draft_thread(draft_id, message)
                if not (isinstance(reference, tuple) and len(reference) == 2):
                    raise RuntimeError("Slack did not return a draft thread reference")
                await update_draft_thread_metadata(engine, draft_id, *reference)
                sent = True
            except Exception:
                log.exception("loop_b.tiktok_thread_failed", draft_id=draft_id)
                sent = False
        else:
            sent = await notifier.notify_founder(message)
        if sent:
            persisted += 1
            log.info("loop_b.dm_sent", action=action, draft_id=draft_id)
        else:
            log.warning("loop_b.dm_failed", action=action, draft_id=draft_id)

    summary = format_summary_dm(drafts_persisted=persisted, drafts_attempted=len(_TODAYS_PLAN))
    await notifier.notify_founder(summary)
    await _record_loop_b_metric(engine, persisted)
    log.info("loop_b.complete", persisted=persisted, attempted=len(_TODAYS_PLAN))
    return persisted
