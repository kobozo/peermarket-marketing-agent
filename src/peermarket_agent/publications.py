"""Durable, idempotent persistence for Meta publication state."""

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class MetaPublication:
    draft_id: int
    state: str | None = None
    external_ids: dict = field(default_factory=dict)
    external_statuses: dict = field(default_factory=dict)
    failure: dict | None = None
    approved_budget_cents: int | None = None
    ads_manager_url: str | None = None
    created_at: datetime | None = field(default=None, compare=False)
    updated_at: datetime | None = field(default=None, compare=False)


async def get_meta_publication(
    engine: AsyncEngine, draft_id: int
) -> MetaPublication | None:
    """Return the publication recorded for a draft, if one exists."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT draft_id, state, "
                    "CASE WHEN external_id IS NOT NULL "
                    "THEN jsonb_build_object('ad_id', external_id) "
                    "ELSE '{}'::JSONB END || COALESCE(external_ids, '{}'::JSONB) "
                    "AS external_ids, external_statuses, failure, "
                    "approved_budget_cents, ads_manager_url, published_at AS created_at, "
                    "updated_at FROM publications WHERE draft_id = :draft_id"
                ),
                {"draft_id": draft_id},
            )
        ).mappings().one_or_none()
    if row is None:
        return None
    values = dict(row)
    values["external_ids"] = values["external_ids"] or {}
    values["external_statuses"] = values["external_statuses"] or {}
    return MetaPublication(**values)


async def upsert_meta_publication(
    engine: AsyncEngine, publication: MetaPublication
) -> None:
    """Persist progress while merging identifiers retained from earlier attempts."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO publications "
                "(draft_id, channel, state, external_ids, external_statuses, failure, "
                "approved_budget_cents, ads_manager_url, updated_at) VALUES "
                "(:draft_id, 'meta', :state, CAST(:external_ids AS JSONB), "
                "CAST(:external_statuses AS JSONB), CAST(:failure AS JSONB), "
                ":approved_budget_cents, :ads_manager_url, NOW()) "
                "ON CONFLICT (draft_id) WHERE draft_id IS NOT NULL DO UPDATE SET "
                "state = COALESCE(EXCLUDED.state, publications.state), "
                "external_ids = COALESCE(publications.external_ids, '{}'::JSONB) "
                "|| EXCLUDED.external_ids, "
                "external_statuses = COALESCE(publications.external_statuses, '{}'::JSONB) "
                "|| EXCLUDED.external_statuses, "
                "failure = COALESCE(EXCLUDED.failure, publications.failure), "
                "approved_budget_cents = COALESCE(EXCLUDED.approved_budget_cents, "
                "publications.approved_budget_cents), "
                "ads_manager_url = COALESCE(EXCLUDED.ads_manager_url, publications.ads_manager_url), "
                "updated_at = NOW()"
            ),
            {
                "draft_id": publication.draft_id,
                "state": publication.state,
                "external_ids": json.dumps(publication.external_ids or {}),
                "external_statuses": json.dumps(publication.external_statuses or {}),
                "failure": json.dumps(publication.failure) if publication.failure is not None else None,
                "approved_budget_cents": publication.approved_budget_cents,
                "ads_manager_url": publication.ads_manager_url,
            },
        )


async def mark_meta_publication_active(
    engine: AsyncEngine, draft_id: int, statuses: dict
) -> None:
    """Mark a reconciled Meta hierarchy active and clear any prior failure."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE publications SET state = 'active', "
                "external_statuses = COALESCE(external_statuses, '{}'::JSONB) "
                "|| CAST(:external_statuses AS JSONB), failure = NULL, updated_at = NOW() "
                "WHERE draft_id = :draft_id"
            ),
            {
                "draft_id": draft_id,
                "external_statuses": json.dumps(statuses or {}),
            },
        )
