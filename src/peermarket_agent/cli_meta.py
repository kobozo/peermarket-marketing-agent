"""Operator CLI for safely reconciling existing Meta resources."""

import asyncio
import json

import click
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.config import Settings, get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.meta_pipeline import (
    TerminalReplacementOperationalError,
    process_approved_meta_draft,
    replace_terminal_meta_draft,
)
from peermarket_agent.publications import (
    get_meta_publication,
)
from peermarket_agent.slack_notifier import SlackNotifier


def _non_empty_id(_context: click.Context, parameter: click.Parameter, value: str | None) -> str:
    if value is None or not value.strip():
        raise click.BadParameter("must be a non-empty ID", param=parameter)
    if value != value.strip():
        raise click.BadParameter("must not have leading or trailing whitespace", param=parameter)
    return value


async def reconcile_draft(
    *,
    engine: AsyncEngine,
    draft_id: int,
    supplied_ids: dict[str, str],
    settings: Settings,
    notifier: SlackNotifier,
    dry_run: bool,
) -> list[str]:
    """Preflight exact IDs, then hand reconciliation to the production pipeline."""
    publication = await get_meta_publication(engine, draft_id)
    stored_ids = publication.external_ids if publication is not None else {}
    for key, supplied_value in supplied_ids.items():
        stored_value = stored_ids.get(key)
        if stored_value is not None and stored_value != supplied_value:
            raise ValueError(
                f"refusing reconciliation: supplied {key} {supplied_value!r} "
                f"conflicts with stored value {stored_value!r}"
            )
    if dry_run:
        state = publication.state if publication is not None else "not recorded"
        statuses = publication.external_statuses if publication is not None else {}
        return [
            f"Draft #{draft_id} reconciliation dry run",
            f"Publication state: {state}",
            f"Observed statuses: {json.dumps(statuses, sort_keys=True)}",
            "No changes made.",
        ]

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=settings,
        notifier=notifier,
        reconciliation_ids=supplied_ids,
    )
    return [f"Draft #{draft_id} dispatched to the production reconciliation pipeline."]


@click.group()
def cli() -> None:
    """Safe Meta publication operations."""


@cli.command("reconcile-draft")
@click.option("--draft-id", required=True, type=click.IntRange(min=1))
@click.option("--campaign-id", required=True, type=str, callback=_non_empty_id)
@click.option("--adset-id", required=True, type=str, callback=_non_empty_id)
@click.option("--creative-id", required=True, type=str, callback=_non_empty_id)
@click.option("--ad-id", required=True, type=str, callback=_non_empty_id)
@click.option("--dry-run", is_flag=True, help="Validate and display stored state only.")
def reconcile_draft_command(
    draft_id: int,
    campaign_id: str,
    adset_id: str,
    creative_id: str,
    ad_id: str,
    dry_run: bool,
) -> None:
    """Reconcile one approved draft with an existing Meta hierarchy."""
    settings = get_settings()
    notifier = SlackNotifier(
        bot_token=settings.slack_bot_token,
        founder_user_id=settings.slack_founder_user_id,
    )
    try:
        lines = asyncio.run(
            reconcile_draft(
                engine=get_engine(),
                draft_id=draft_id,
                supplied_ids={
                    "campaign_id": campaign_id,
                    "ad_set_id": adset_id,
                    "creative_id": creative_id,
                    "ad_id": ad_id,
                },
                settings=settings,
                notifier=notifier,
                dry_run=dry_run,
            )
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    for line in lines:
        click.echo(line)


@cli.command("replace-terminal-draft")
@click.option("--draft-id", required=True, type=click.IntRange(min=1))
@click.option("--campaign-id", required=True, type=str, callback=_non_empty_id)
@click.option("--adset-id", required=True, type=str, callback=_non_empty_id)
@click.option("--creative-id", required=True, type=str, callback=_non_empty_id)
@click.option("--ad-id", required=True, type=str, callback=_non_empty_id)
def replace_terminal_draft_command(
    draft_id: int,
    campaign_id: str,
    adset_id: str,
    creative_id: str,
    ad_id: str,
) -> None:
    """Replace an exact stored hierarchy only when every resource is terminal."""
    settings = get_settings()
    notifier = SlackNotifier(
        bot_token=settings.slack_bot_token,
        founder_user_id=settings.slack_founder_user_id,
    )
    ids = {
        "campaign_id": campaign_id,
        "ad_set_id": adset_id,
        "creative_id": creative_id,
        "ad_id": ad_id,
    }
    try:
        result = asyncio.run(
            replace_terminal_meta_draft(
                engine=get_engine(),
                draft_id=draft_id,
                settings=settings,
                notifier=notifier,
                expected_ids=ids,
            )
        )
    except (ValueError, TerminalReplacementOperationalError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Draft #{draft_id} terminal replacement attempt completed.")
    click.echo(f"Archived IDs: {json.dumps(result.old_ids, sort_keys=True)}")
    click.echo(f"Archived statuses: {json.dumps(result.terminal_statuses, sort_keys=True)}")
    click.echo(f"Current IDs: {json.dumps(result.current_ids, sort_keys=True)}")
    click.echo(f"Current state: {result.state}")
    if result.failure:
        click.echo(f"Current failure: {json.dumps(result.failure, sort_keys=True)}")


if __name__ == "__main__":
    cli()
