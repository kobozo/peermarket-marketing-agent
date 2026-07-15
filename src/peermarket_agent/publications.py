"""Durable, idempotent persistence for Meta publication state."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

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
    replacement_history: list[dict] = field(default_factory=list)
    created_at: datetime | None = field(default=None, compare=False)
    updated_at: datetime | None = field(default=None, compare=False)


class MetaReplacementHistoryError(RuntimeError):
    """A started replacement attempt could not be durably finalized."""


async def get_meta_publication(engine: AsyncEngine, draft_id: int) -> MetaPublication | None:
    """Return the publication recorded for a draft, if one exists."""
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT draft_id, state, "
                        "CASE WHEN external_id IS NOT NULL "
                        "THEN jsonb_build_object('ad_id', external_id) "
                        "ELSE '{}'::JSONB END || COALESCE(external_ids, '{}'::JSONB) "
                        "AS external_ids, external_statuses, failure, "
                        "approved_budget_cents, ads_manager_url, replacement_history, published_at AS created_at, "
                        "updated_at FROM publications WHERE draft_id = :draft_id"
                    ),
                    {"draft_id": draft_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    values = dict(row)
    values["external_ids"] = values["external_ids"] or {}
    values["external_statuses"] = values["external_statuses"] or {}
    values["replacement_history"] = values.get("replacement_history") or []
    return MetaPublication(**values)


async def upsert_meta_publication(engine: AsyncEngine, publication: MetaPublication) -> None:
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
                "failure": json.dumps(publication.failure)
                if publication.failure is not None
                else None,
                "approved_budget_cents": publication.approved_budget_cents,
                "ads_manager_url": publication.ads_manager_url,
            },
        )


async def mark_meta_publication_active(engine: AsyncEngine, draft_id: int, statuses: dict) -> None:
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


async def begin_meta_terminal_replacement(
    engine: AsyncEngine,
    draft_id: int,
    expected_ids: dict[str, str],
    terminal_statuses: dict[str, dict[str, str]],
) -> str:
    """Archive the exact current hierarchy and clear it in one guarded transition."""
    attempt_id = str(uuid4())
    history_entry = {
        "attempt_id": attempt_id,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "old_ids": expected_ids,
        "terminal_statuses": terminal_statuses,
        "replacement_ids": {},
        "state": "creating",
    }
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "UPDATE publications SET external_id = NULL, external_ids = '{}'::JSONB, "
                "external_statuses = '{}'::JSONB, state = 'creating', failure = NULL, "
                "ads_manager_url = NULL, replacement_history = "
                "COALESCE(replacement_history, '[]'::JSONB) || CAST(:entry AS JSONB), "
                "updated_at = NOW() WHERE draft_id = :draft_id AND "
                "(CASE WHEN external_id IS NOT NULL THEN jsonb_build_object('ad_id', external_id) "
                "ELSE '{}'::JSONB END || COALESCE(external_ids, '{}'::JSONB)) = "
                "CAST(:expected_ids AS JSONB)"
            ),
            {
                "draft_id": draft_id,
                "expected_ids": json.dumps(expected_ids),
                "entry": json.dumps([history_entry]),
            },
        )
        if result.rowcount != 1:
            raise ValueError("refusing replacement: stored Meta IDs changed")
    return attempt_id


async def record_meta_replacement_result(
    engine: AsyncEngine,
    draft_id: int,
    attempt_id: str,
    *,
    state: str,
    failure: dict | None,
) -> None:
    """Finalize one identified replacement attempt in place."""
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "UPDATE publications SET replacement_history = "
                "COALESCE((SELECT jsonb_agg(CASE WHEN item.value->>'attempt_id' = :attempt_id "
                "THEN item.value || jsonb_build_object("
                "'replacement_ids', COALESCE(publications.external_ids, '{}'::JSONB), "
                "'state', CAST(:state AS TEXT), 'failure', CAST(:failure AS JSONB), "
                "'finished_at', CAST(:finished_at AS TEXT)) ELSE item.value END "
                "ORDER BY item.ordinality) "
                "FROM jsonb_array_elements(COALESCE(publications.replacement_history, '[]'::JSONB)) "
                "WITH ORDINALITY AS item(value, ordinality)), '[]'::JSONB), updated_at = NOW() "
                "WHERE draft_id = :draft_id AND (SELECT COUNT(*) FROM "
                "jsonb_array_elements(COALESCE(replacement_history, '[]'::JSONB)) AS matching "
                "WHERE matching->>'attempt_id' = :attempt_id) = 1 AND EXISTS (SELECT 1 FROM "
                "jsonb_array_elements(COALESCE(replacement_history, '[]'::JSONB)) AS unfinished "
                "WHERE unfinished->>'attempt_id' = :attempt_id "
                "AND unfinished->>'finished_at' IS NULL)"
            ),
            {
                "draft_id": draft_id,
                "attempt_id": attempt_id,
                "state": state,
                "failure": json.dumps(failure) if failure is not None else None,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        if result.rowcount != 1:
            raise MetaReplacementHistoryError(
                f"replacement attempt was not found or unfinished exactly once for draft #{draft_id}"
            )
