"""CLI: peermarket-agent-draft <action_type> [args]

Orchestrates: generate → score → persist-if-passing.
"""

import asyncio
from typing import Any

import anthropic
import click
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.balance_alert import NUDGE_MESSAGE, is_credit_balance_error
from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.config import get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.prompts.email_re_engagement import generate_email
from peermarket_agent.prompts.meta_ad_creative import pick_audience
from peermarket_agent.prompts.seo_pr import generate_seo_meta
from peermarket_agent.prompts.tiktok_post import generate_tiktok_post
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


async def recent_relevant_learnings(
    engine: AsyncEngine,
    *,
    channel: str,
    objective: str,
    language: str,
    audience: str,
) -> tuple[str, ...]:
    """Return only bounded, eligible, exact-dimension reusable learnings."""
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT text FROM learnings WHERE "
                        "split_part(scope, ':', 1) IN ('delivery', 'conversion') "
                        "AND split_part(scope, ':', 2)=:channel "
                        "AND split_part(scope, ':', 3)=:objective "
                        "AND split_part(scope, ':', 4)=:language "
                        "AND split_part(scope, ':', 5)=:audience "
                        "AND evidence_links->'decision'->>'eligible'='true' "
                        "AND trim(BOTH '0.' FROM COALESCE("
                        "evidence_links->'decision'->'outcome'->>'absolute_difference', ''))<>'' "
                        "ORDER BY id DESC LIMIT 5"
                    ),
                    {
                        "channel": channel,
                        "objective": objective,
                        "language": language,
                        "audience": audience,
                    },
                )
            )
            .scalars()
            .all()
        )
    return tuple(rows)


def _human_cta_to_meta_enum(human: str) -> str:
    mapping = {
        "Learn More": "LEARN_MORE",
        "Sign Up": "SIGN_UP",
        "Shop Now": "SHOP_NOW",
        "Get Started": "GET_STARTED",
    }
    return mapping[human]


async def _produce_copy_for_action(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    action_type_name: str,
    **action_args: Any,
) -> tuple[str, str, str, int, dict]:
    """Return (channel, language, copy_text, generation_cost_cents, metadata)."""
    if action_type_name == "tiktok_post_organic":
        post = await generate_tiktok_post(
            claude=claude,
            brand_voice_md=brand_voice_md,
            language=action_args["language"],
            theme=action_args.get("theme", "declutter"),
        )
        copy_text = f"{post.hook}\n\n{post.body}\n\n{post.cta}"
        return "tiktok", action_args["language"], copy_text, post.cost_cents, {}
    elif action_type_name == "email_re_engagement":
        email = await generate_email(
            claude=claude,
            brand_voice_md=brand_voice_md,
            language=action_args["language"],
            audience=action_args.get("audience", "dormant_signups"),
        )
        copy_text = f"Subject: {email.subject}\n\n{email.body}"
        return "email", action_args["language"], copy_text, email.cost_cents, {}
    elif action_type_name == "seo_pr":
        meta = await generate_seo_meta(
            claude=claude,
            brand_voice_md=brand_voice_md,
            language=action_args["language"],
            page_path=action_args["page_path"],
            page_subject=action_args.get("page_subject", ""),
        )
        copy_text = (
            f'<title>{meta.title}</title>\n<meta name="description" content="{meta.description}">'
        )
        return "seo", action_args["language"], copy_text, meta.cost_cents, {}
    elif action_type_name == "meta_ad_creative":
        from peermarket_agent.prompts.meta_ad_creative import (
            generate_meta_ad_creative,
            pick_audience,
        )

        # If caller didn't specify, randomly pick; else use the provided key
        audience_key = action_args.get("audience_profile_key") or pick_audience()
        ad = await generate_meta_ad_creative(
            claude=claude,
            brand_voice_md=brand_voice_md,
            language=action_args["language"],
            audience_profile_key=audience_key,
            learnings=tuple(action_args.get("learnings") or ()),
        )
        copy_text = (
            f"Audience: {audience_key}\n"
            f"Headline: {ad.headline}\n"
            f"Description: {ad.description}\n"
            f"CTA: {ad.cta_label}\n"
            f"Suggested daily budget: €{ad.suggested_daily_budget_eur}\n\n"
            f"Primary text:\n{ad.primary_text}"
        )
        metadata = {
            "audience_profile_key": audience_key,
            "objective": "OUTCOME_TRAFFIC",
            "headline": ad.headline,
            "description": ad.description,
            "cta_label": ad.cta_label,
            "cta_type": _human_cta_to_meta_enum(ad.cta_label),
            "suggested_daily_budget_eur": ad.suggested_daily_budget_eur,
            "primary_text": ad.primary_text,
        }
        return "meta", action_args["language"], copy_text, ad.cost_cents, metadata
    else:
        raise ValueError(f"unsupported action_type: {action_type_name!r}")


async def run_draft_command(
    *,
    engine: AsyncEngine,
    claude: ClaudeClient,
    action_type_name: str,
    notifier: SlackNotifier | None = None,
    **action_args: Any,
) -> int | None:
    """End-to-end: generate, score, persist if passing. Returns draft id or None.

    On Anthropic credit-balance-low errors, DMs the founder with a topup
    nudge (if `notifier` is provided) then re-raises.
    """
    try:
        brand_voice_md = load_brand_voice()
        if action_type_name == "meta_ad_creative":
            audience_key = action_args.get("audience_profile_key") or pick_audience()
            action_args["audience_profile_key"] = audience_key
            action_args["learnings"] = await recent_relevant_learnings(
                engine,
                channel="meta",
                objective="OUTCOME_TRAFFIC",
                language=action_args["language"],
                audience=audience_key,
            )
        channel, language, copy_text, gen_cost, metadata = await _produce_copy_for_action(
            claude=claude,
            brand_voice_md=brand_voice_md,
            action_type_name=action_type_name,
            **action_args,
        )
        score, notes = await score_draft(
            claude=claude,
            brand_voice_md=brand_voice_md,
            copy=copy_text,
        )
        log.info(
            "draft.scored",
            action=action_type_name,
            score=score,
            threshold=BRAND_SCORE_THRESHOLD,
            notes=notes,
        )
        if score < BRAND_SCORE_THRESHOLD:
            log.info("draft.rejected_by_gate", action=action_type_name, score=score)
            return None
        draft = Draft(
            action_type_name=action_type_name,
            channel=channel,
            language=language,
            copy=copy_text,
            asset_path=None,
            generation_cost_cents=gen_cost,
            brand_score=score,
            visual_truthfulness_pass=True,  # text-only in Phase 1a
            metadata=metadata,
        )
        draft_id = await persist_draft(engine, draft)
        log.info("draft.persisted", action=action_type_name, draft_id=draft_id)
        return draft_id
    except anthropic.BadRequestError as exc:
        if is_credit_balance_error(exc) and notifier is not None:
            await notifier.notify_founder(NUDGE_MESSAGE)
        raise


@click.command()
@click.argument("action_type")
@click.option("--language", default="NL", show_default=True)
@click.option("--theme", default="declutter", help="(tiktok only)")
@click.option("--audience", default="dormant_signups", help="(email only)")
@click.option("--page-path", default=None, help="(seo only)")
@click.option("--page-subject", default="", help="(seo only)")
@click.option(
    "--audience-profile",
    default=None,
    help="(meta_ad_creative only) declutterers | trust_conscious_locals; random if omitted",
)
def cli(
    action_type: str,
    language: str,
    theme: str,
    audience: str,
    page_path: str | None,
    page_subject: str,
    audience_profile: str | None,
) -> None:
    """Generate one draft of the given action type. Persists if brand_score >= 80."""
    settings = get_settings()
    engine = get_engine()
    claude = ClaudeClient(api_key=settings.anthropic_api_key)
    notifier = SlackNotifier(
        bot_token=settings.slack_bot_token,
        founder_user_id=settings.slack_founder_user_id,
    )
    kwargs: dict[str, Any] = {"language": language}
    if action_type == "tiktok_post_organic":
        kwargs["theme"] = theme
    elif action_type == "email_re_engagement":
        kwargs["audience"] = audience
    elif action_type == "seo_pr":
        if not page_path:
            raise click.UsageError("--page-path required for seo_pr")
        kwargs["page_path"] = page_path
        kwargs["page_subject"] = page_subject
    elif action_type == "meta_ad_creative":
        kwargs["audience_profile_key"] = audience_profile  # may be None, generator picks
    result = asyncio.run(
        run_draft_command(
            engine=engine,
            claude=claude,
            notifier=notifier,
            action_type_name=action_type,
            **kwargs,
        )
    )
    if result is None:
        click.echo("Draft rejected by brand-quality gate (score < 80)")
    else:
        click.echo(f"Draft persisted: id={result}")


if __name__ == "__main__":
    cli()
