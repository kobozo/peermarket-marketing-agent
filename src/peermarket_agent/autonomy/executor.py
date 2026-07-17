"""Fenced execution and compensation for autonomous Meta lifecycle actions."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text

from peermarket_agent.autonomy.contracts import ActionStatus, DecisionKind
from peermarket_agent.autonomy.replacements import (
    LocaleCreative,
    ReplacementSource,
    build_replacement,
)
from peermarket_agent.autonomy.snapshot import build_autonomy_snapshot
from peermarket_agent.autonomy.store import (
    begin_execution,
    block_campaign_for_reconciliation,
    campaign_history,
    finish_action,
    release_action,
)
from peermarket_agent.config import Settings
from peermarket_agent.meta_ads import (
    MetaBundleLocale,
    MetaConfig,
    activate_meta_ad,
    get_meta_ad_statuses,
    get_meta_allocation_state,
    get_meta_budget_state,
    get_meta_replacement_bundle_statuses,
    pause_meta_replacement_bundle,
    set_meta_ad_status,
    set_meta_adset_daily_budget,
)
from peermarket_agent.meta_pipeline import publish_replacement_paused

_HEARTBEAT_SECONDS = 30.0
_RATE_LIMIT_CODES = {4, 17, 32, 613}
_ACTIVE_EFFECTIVE = {"ACTIVE", "IN_PROCESS", "PENDING_REVIEW"}
_PAUSED_EFFECTIVE = {
    "PAUSED",
    "CAMPAIGN_PAUSED",
    "ADSET_PAUSED",
    "PENDING_REVIEW",
    "IN_PROCESS",
    "PREAPPROVED",
    "PENDING_BILLING_INFO",
}
_WRITE_STARTED: ContextVar[bool] = ContextVar("autonomy_write_started", default=False)


class ExecutionStatus(StrEnum):
    REFUSED = "refused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    reason: str
    before_state: Any = None
    after_state: Any = None
    rollback_result: Any = None
    retry_at: datetime | None = None


class _AmbiguousExternalWrite(RuntimeError):
    """An external mutation failed without proving whether Meta applied it."""


class _SagaFailure(RuntimeError):
    def __init__(self, cause: BaseException, rollback_result: Any) -> None:
        self.cause = cause
        self.rollback_result = rollback_result
        super().__init__(type(cause).__name__)


class MetaExecutionAdapter:
    """Production bridge from executor verbs to the Task 4/5 interfaces."""

    def __init__(self, settings: Settings, claude: Any) -> None:
        self.settings = settings
        self.claude = claude
        self.config = MetaConfig(
            app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret,
            system_user_token=settings.meta_system_user_token,
            ad_account_id=settings.meta_ad_account_id,
            page_id=settings.meta_page_id,
        )
        self._drafts: dict[str, Any] = {}
        self._identities: dict[str, dict[str, Any]] = {}

    async def read_source(self, campaign_id: str, *, ids: Mapping[str, str]) -> dict[str, Any]:
        statuses = await get_meta_ad_statuses(self.config, dict(ids))
        budget = await get_meta_budget_state(self.config, dict(ids))
        return {
            "campaign_id": campaign_id,
            "ad_set_id": ids["ad_set_id"],
            "ad_id": ids["ad_id"],
            "status": statuses["ad"]["status"],
            "effective_status": statuses["ad"]["effective_status"],
            "hierarchy": statuses,
            "budget_cents": budget["ad_set"]["daily_budget"],
        }

    async def pause_source(self, *, source: Mapping[str, Any]) -> Any:
        return await set_meta_ad_status(self.config, source["ad_id"], "PAUSED")

    async def set_budget(self, *, ad_set_id: str, cents: int) -> Any:
        return await set_meta_adset_daily_budget(self.config, ad_set_id, cents)

    async def read_allocation(self, *, allocation: Mapping[str, Any]) -> Any:
        result = await get_meta_allocation_state(
            self.config,
            allocation["campaign_id"],
            allocation["ad_set_id"],
            allocation["ad_id"],
        )
        return dict(result) | {"variant_id": allocation["variant_id"]}

    async def build(self, *, engine: Any, claim: Any, source: ReplacementSource) -> Any:
        return await build_replacement(engine, self.claude, source, claim.decision, claim=claim)

    async def create_paused(self, *, engine: Any, claim: Any, draft: Any) -> Any:
        publication = await publish_replacement_paused(
            engine=engine, settings=self.settings, claim=claim, draft=draft
        )
        self._drafts[publication.campaign_id] = draft
        async with engine.connect() as conn:
            progress = await conn.scalar(
                text(
                    "SELECT progress FROM autonomous_replacement_publications WHERE action_id=:id"
                ),
                {"id": claim.id},
            )
        progress = progress or {}
        ctas = {
            "Learn More": "LEARN_MORE",
            "Sign Up": "SIGN_UP",
            "Shop Now": "SHOP_NOW",
            "Get Started": "GET_STARTED",
        }
        self._identities[publication.campaign_id] = {
            "creative_ids": publication.creative_ids,
            "landing_page_url": draft.landing_page_url,
            "locales": {
                locale: MetaBundleLocale(
                    item.primary_text,
                    item.headline,
                    item.description,
                    ctas[item.cta_label],
                    None,
                )
                for locale, item in draft.locales.items()
            },
            "image_hashes": {
                locale: progress[f"image_hash:{locale}"]
                for locale in ("NL", "FR", "EN")
                if progress.get(f"image_hash:{locale}")
            },
        }
        return publication

    async def read_replacement(
        self,
        campaign_id: str,
        ad_set_id: str,
        ad_ids: Mapping[str, str],
        creative_ids: Mapping[str, str] | None = None,
    ) -> Any:
        identity = self._identities.get(campaign_id, {})
        return await get_meta_replacement_bundle_statuses(
            self.config,
            campaign_id,
            ad_set_id,
            ad_ids,
            **(identity or {"creative_ids": creative_ids}),
        )

    async def activate_replacement(
        self, campaign_id: str, ad_set_id: str, ad_ids: Mapping[str, str]
    ) -> Any:
        first = ad_ids["NL"]
        result = await activate_meta_ad(
            self.config,
            {"campaign_id": campaign_id, "ad_set_id": ad_set_id, "ad_id": first},
        )
        for locale in ("FR", "EN"):
            await set_meta_ad_status(self.config, ad_ids[locale], "ACTIVE")
        return result

    async def pause_replacement(
        self, campaign_id: str, ad_set_id: str, ad_ids: Mapping[str, str]
    ) -> Any:
        return await pause_meta_replacement_bundle(self.config, campaign_id, ad_set_id, ad_ids)

    async def pause_persisted(self, *, engine: Any, claim: Any) -> Any:
        async with engine.connect() as conn:
            progress = await conn.scalar(
                text(
                    "SELECT progress FROM autonomous_replacement_publications WHERE action_id=:id"
                ),
                {"id": claim.id},
            )
        progress = progress or {}
        campaign_id, ad_set_id = progress.get("campaign_id"), progress.get("ad_set_id")
        ad_ids = {
            locale: progress[f"ad_id:{locale}"]
            for locale in ("NL", "FR", "EN")
            if progress.get(f"ad_id:{locale}")
        }
        if not campaign_id or not ad_set_id or set(ad_ids) != {"NL", "FR", "EN"}:
            return {
                "verified": False,
                "observed": {},
                "pause_errors": {"ids": "persisted IDs incomplete"},
            }
        mutation = await pause_meta_replacement_bundle(self.config, campaign_id, ad_set_id, ad_ids)
        observed = await self.read_replacement(campaign_id, ad_set_id, ad_ids)
        draft = self._drafts.get(campaign_id)
        budget = draft.daily_budget_eur * 100 if draft is not None else -1
        return {
            "verified": _bundle_verified(observed, False, budget),
            "mutation": mutation,
            "observed": observed,
        }


async def execute_production_claim(
    engine: Any, settings: Settings, claude: Any, claim: Any, now: datetime
) -> ExecutionResult:
    """Task 7 entrypoint wired exclusively to the production Task 4/5 adapters."""
    return await execute_claim(
        engine, settings, MetaExecutionAdapter(settings, claude), None, claim, now
    )


def _replacement_source(decision: Any) -> ReplacementSource:
    raw = decision.evidence.get("source")
    if not isinstance(raw, Mapping):
        raise ValueError("replacement decision lacks frozen source")
    locales = raw.get("locales")
    if not isinstance(locales, Mapping):
        raise ValueError("replacement decision lacks frozen locales")
    return ReplacementSource(
        draft_id=raw["draft_id"],
        campaign_id=raw["campaign_id"],
        experiment_id=raw["experiment_id"],
        changed_dimension=raw["changed_dimension"],
        locales={key: LocaleCreative(**value) for key, value in locales.items()},
        audience_profile_key=raw["audience_profile_key"],
        image_prompt=raw["image_prompt"],
        asset_path=raw["asset_path"],
        daily_budget_eur=raw["daily_budget_eur"],
        landing_page_url=raw["landing_page_url"],
        publication_id=raw["publication_id"],
        objective=raw["objective"],
        current_meta_ids=raw["current_meta_ids"],
    )


def _setting(settings: object, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


async def _call(target: object, name: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(target, name)
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return await function(*args, **kwargs)
    if not any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    value = function(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _rate_limited(exc: BaseException) -> bool:
    return (
        getattr(exc, "http_status", None) == 429
        or getattr(exc, "api_error_code", None) in _RATE_LIMIT_CODES
        or getattr(exc, "error_code", None) in _RATE_LIMIT_CODES
    )


def _transient(exc: BaseException) -> bool:
    return (
        _rate_limited(exc)
        or isinstance(exc, (TimeoutError, ConnectionError))
        or getattr(exc, "http_status", None) in {500, 502, 503, 504}
    )


async def _renew_action(engine: Any, claim: Any, seconds: int = 300) -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()+make_interval(secs=>:seconds), updated_at=NOW() WHERE id=:id AND status='executing' AND lease_owner=:owner AND lease_token=:token AND lease_expires_at>NOW()"
            ),
            {
                "seconds": seconds,
                "id": claim.id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError("execution lease ownership was lost")


async def _external(
    db_engine: Any, execution_claim: Any, target: object, name: str, *args: Any, **kwargs: Any
) -> Any:
    await _renew_action(db_engine, execution_claim)
    return await _call(target, name, *args, **kwargs)


async def _write_external(
    db_engine: Any, execution_claim: Any, target: object, name: str, *args: Any, **kwargs: Any
) -> Any:
    _WRITE_STARTED.set(True)
    try:
        return await _external(db_engine, execution_claim, target, name, *args, **kwargs)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        raise _AmbiguousExternalWrite(name) from exc


async def _heartbeat(engine: Any, claim: Any, lost: asyncio.Event) -> None:
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            await _renew_action(engine, claim)
    except asyncio.CancelledError:
        raise
    except Exception:
        lost.set()


async def _publication(engine: Any, campaign_id: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT p.id AS publication_id, p.draft_id, p.state, p.external_ids, p.approved_budget_cents, p.performance FROM publications p WHERE p.channel='meta' AND p.external_ids->>'campaign_id'=:campaign ORDER BY p.updated_at DESC LIMIT 1"
                    ),
                    {"campaign": campaign_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _ids(publication: Mapping[str, Any]) -> dict[str, str]:
    raw = publication.get("external_ids") or {}
    return {key: str(raw[key]) for key in ("campaign_id", "ad_set_id", "ad_id") if raw.get(key)}


def _source_ids(source: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: str(source[key]) for key in ("campaign_id", "ad_set_id", "ad_id") if source.get(key)
    }


def _source_ok(
    source: Mapping[str, Any], campaign: str, ids: Mapping[str, str], budget: int
) -> bool:
    hierarchy = source.get("hierarchy") or {}
    return (
        str(source.get("campaign_id")) == campaign
        and all(
            not ids.get(k) or str(source.get(k)) == ids[k]
            for k in ("campaign_id", "ad_set_id", "ad_id")
        )
        and source.get("budget_cents", source.get("daily_budget")) == budget
        and source.get("status") == "ACTIVE"
        and source.get("effective_status") in _ACTIVE_EFFECTIVE
        and set(hierarchy) == {"campaign", "ad_set", "ad"}
        and all(
            item.get("status") == "ACTIVE" and item.get("effective_status") in _ACTIVE_EFFECTIVE
            for item in hierarchy.values()
        )
    )


def _paused_source_ok(source: Mapping[str, Any], ids: Mapping[str, str], budget: int) -> bool:
    hierarchy = source.get("hierarchy") or {}
    return (
        _source_ids(source) == dict(ids)
        and source.get("budget_cents") == budget
        and set(hierarchy) == {"campaign", "ad_set", "ad"}
        and hierarchy["campaign"].get("status") == "ACTIVE"
        and hierarchy["campaign"].get("effective_status") in _ACTIVE_EFFECTIVE
        and hierarchy["ad_set"].get("status") == "ACTIVE"
        and hierarchy["ad_set"].get("effective_status") in _ACTIVE_EFFECTIVE
        and hierarchy["ad"].get("status") == "PAUSED"
        and hierarchy["ad"].get("effective_status") in _PAUSED_EFFECTIVE
        and source.get("status") == "PAUSED"
        and source.get("effective_status") in _PAUSED_EFFECTIVE
    )


def _history_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        if row.get("status") == "succeeded":
            events.append({"kind": row.get("kind"), "at": row.get("updated_at")})
        for event in row.get("budget_events", ()):
            events.append({"kind": "budget", "at": event["created_at"], **event})
    return events


def _intent(claim: Any) -> dict[str, Any]:
    return {
        "kind": claim.kind.value,
        "campaign_id": claim.campaign_id,
        "decision_id": claim.decision_id,
        "old_budget_cents": claim.decision.old_budget_cents,
        "new_budget_cents": claim.decision.new_budget_cents,
        "allocations": _plain(claim.decision.allocations),
        "decision_key": claim.decision.idempotency_key,
    }


def _policy_reason(
    claim: Any,
    settings: Any,
    publication: Mapping[str, Any],
    source: Mapping[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
) -> str | None:
    decision = claim.decision
    if decision.kind is DecisionKind.REPLACE:
        frozen = decision.evidence.get("source")
        current = frozen.get("current_meta_ids") if isinstance(frozen, Mapping) else None
        external = publication.get("external_ids") or {}
        if (
            not isinstance(current, Mapping)
            or frozen.get("draft_id") != publication.get("draft_id")
            or frozen.get("publication_id") != publication.get("publication_id")
            or frozen.get("daily_budget_eur") * 100 != publication.get("approved_budget_cents")
            or current.get("campaign_id") != external.get("campaign_id")
            or current.get("ad_set_id") != external.get("ad_set_id")
            or external.get("ad_id") not in (current.get("ad_ids") or {}).values()
            or (
                external.get("creative_id") is not None
                and external.get("creative_id") not in (current.get("creative_ids") or {}).values()
            )
        ):
            return "replacement_source_changed"
    try:
        snapshot = build_autonomy_snapshot(
            publication,
            decision.evidence.get("variants", ()),
            replacement_source=decision.evidence.get("source"),
            allow_replacement=decision.kind is DecisionKind.REPLACE,
            reallocation=(
                {
                    "old_budget_cents": decision.old_budget_cents,
                    "new_budget_cents": decision.new_budget_cents,
                    "allocations": decision.allocations,
                }
                if decision.kind is DecisionKind.REALLOCATE
                else None
            ),
        )
    except (TypeError, ValueError):
        return "missing_snapshot"
    frozen_basis = decision.evidence.get("frozen_basis")
    if (
        not isinstance(frozen_basis, Mapping)
        or snapshot.get("frozen_basis") != frozen_basis
        or publication.get("external_ids") != frozen_basis.get("external_ids")
        or publication.get("approved_budget_cents") != frozen_basis.get("approved_budget_cents")
    ):
        return "frozen_basis_changed"
    captured_at = snapshot["captured_at"]
    if (
        snapshot.get("snapshot_id") != decision.evidence.get("snapshot_id")
        or captured_at is None
        or captured_at.tzinfo is None
        or not timedelta(0)
        <= now - captured_at
        <= timedelta(hours=_setting(settings, "performance_snapshot_max_age_hours", 2))
        or snapshot.get("window_start")
        not in {
            decision.window_start,
            decision.window_start.isoformat(),
        }
        or snapshot.get("window_end") not in {decision.window_end, decision.window_end.isoformat()}
        or snapshot.get("complete") is not True
        or snapshot.get("attribution_complete") is not True
        or snapshot.get("delivery_state") != decision.evidence.get("delivery_state")
        or snapshot.get("variants") != decision.evidence.get("variants")
    ):
        return "stale_snapshot"
    budget = publication.get("approved_budget_cents")
    if not isinstance(budget, int) or not _source_ok(
        source, claim.campaign_id, _ids(publication), budget
    ):
        return "live_state_changed"
    boundary = now - timedelta(hours=_setting(settings, "meta_autonomy_cooldown_hours", 24))
    if any(
        e.get("at")
        and e["at"] > boundary
        and e.get("kind") in {"pause", "replace", "reallocate", "scale"}
        for e in history
    ):
        return "cooldown"
    if decision.kind is DecisionKind.REPLACE:
        replacements = sum(
            e.get("kind") == "replace" and e.get("at") and e["at"] > now - timedelta(hours=24)
            for e in history
        )
        if replacements >= _setting(settings, "meta_autonomy_max_replacements_24h", 1):
            return "replacement_limit"
    if decision.kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE}:
        if decision.old_budget_cents != budget:
            return "budget_changed"
        new = decision.new_budget_cents
        if (
            not isinstance(new, int)
            or new > _setting(settings, "meta_autonomy_max_daily_budget_eur", 20) * 100
        ):
            return "budget_cap"
        if decision.kind is DecisionKind.SCALE:
            opening = next(
                (
                    e["old_budget_cents"]
                    for e in history
                    if e.get("kind") == "budget"
                    and e.get("at")
                    and e["at"] > now - timedelta(hours=24)
                ),
                budget,
            )
            used = sum(
                max(0, e["new_budget_cents"] - e["old_budget_cents"])
                for e in history
                if e.get("kind") == "budget" and e.get("at") and e["at"] > now - timedelta(hours=24)
            )
            cap = opening * _setting(settings, "meta_autonomy_max_increase_percent", 20) // 100
            if new - budget > cap - used or new > opening + cap:
                return "increase_cap"
    return None


def _bundle_ids(bundle: Any) -> dict[str, Any]:
    value = _plain(bundle)
    result = {
        "campaign_id": value["campaign_id"],
        "ad_set_id": value["ad_set_id"],
        "ad_ids": value["ad_ids"],
        "creative_ids": value.get("creative_ids"),
    }
    if set(result["ad_ids"]) != {"NL", "FR", "EN"}:
        raise RuntimeError("replacement_bundle_must_contain_exact_locales")
    creative_ids = value.get("creative_ids")
    if not isinstance(creative_ids, Mapping) or set(creative_ids) != {"NL", "FR", "EN"}:
        raise RuntimeError("replacement_bundle_must_contain_exact_creatives")
    return result


def _bundle_verified(state: Mapping[str, Any], active: bool, budget_cents: int) -> bool:
    keys = {"campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN"}
    if frozenset(state) not in {
        frozenset(keys),
        frozenset(keys | {"creative:NL", "creative:FR", "creative:EN"}),
    }:
        return False
    wanted = "ACTIVE" if active else "PAUSED"
    effective = _ACTIVE_EFFECTIVE if active else _PAUSED_EFFECTIVE
    return state["ad_set"].get("daily_budget") == budget_cents and all(
        state[key].get("status") == wanted and state[key].get("effective_status") in effective
        for key in keys
    )


async def _replace(
    engine: Any, settings: Any, meta: Any, builder: Any, claim: Any, source: Mapping[str, Any]
) -> tuple[Any, Any]:
    builder = builder or meta
    frozen_source = _replacement_source(claim.decision)
    draft = await _call(
        builder, "build", engine=engine, settings=settings, claim=claim, source=frozen_source
    )
    ids = None
    compensated = False
    rollback = None
    try:
        bundle = await _write_external(
            engine, claim, meta, "create_paused", engine=engine, draft=draft, claim=claim
        )
        ids = _bundle_ids(bundle)
        paused = await _external(engine, claim, meta, "read_replacement", **ids)
        budget_cents = frozen_source.daily_budget_eur * 100
        if not _bundle_verified(paused, False, budget_cents):
            raise RuntimeError("replacement_not_paused")
        await _write_external(engine, claim, meta, "activate_replacement", **ids)
        active = await _external(engine, claim, meta, "read_replacement", **ids)
        if not _bundle_verified(active, True, budget_cents):
            raise RuntimeError("replacement_not_active")
        try:
            await _write_external(engine, claim, meta, "pause_source", source=source)
            source_after = await _external(
                engine, claim, meta, "read_source", claim.campaign_id, ids=_source_ids(source)
            )
            frozen_basis = claim.decision.evidence.get("frozen_basis") or {}
            if not _paused_source_ok(
                source_after,
                frozen_basis.get("external_ids") or _source_ids(source),
                frozen_basis.get("approved_budget_cents") or source.get("budget_cents"),
            ):
                raise RuntimeError("source_not_paused")
        except BaseException:
            compensated = True
            try:
                rollback = await _write_external(engine, claim, meta, "pause_replacement", **ids)
                verified = await _external(engine, claim, meta, "read_replacement", **ids)
            except Exception as rollback_error:
                rollback = {"error": type(rollback_error).__name__}
                raise
                verified_safe = _bundle_verified(verified, False, budget_cents)
                rollback = {
                    "mutation": rollback,
                    "verified": verified_safe,
                    "observed": verified,
                }
                if not verified_safe:
                    raise RuntimeError("replacement_rollback_unproven") from None
            raise
        return {"replacement": active, "source": source_after}, rollback
    except BaseException as cause:
        if not compensated:
            try:
                if ids is not None:
                    rollback = await _write_external(
                        engine, claim, meta, "pause_replacement", **ids
                    )
                    verified = await _external(engine, claim, meta, "read_replacement", **ids)
                    verified_safe = _bundle_verified(
                        verified, False, frozen_source.daily_budget_eur * 100
                    )
                    rollback = {
                        "mutation": rollback,
                        "verified": verified_safe,
                        "observed": verified,
                    }
                    if not verified_safe:
                        raise RuntimeError("replacement_rollback_unproven")
                elif hasattr(meta, "pause_persisted"):
                    rollback = await _write_external(
                        engine, claim, meta, "pause_persisted", engine=engine, claim=claim
                    )
                    if rollback.get("verified") is not True:
                        raise RuntimeError("persisted_replacement_rollback_unproven")
            except Exception as rollback_error:
                rollback = {
                    "result": rollback,
                    "error": type(rollback_error).__name__,
                }
        raise _SagaFailure(cause, rollback) from cause


async def execute_claim(
    engine: Any, settings: Any, meta: Any, replacement_builder: Any, claim: Any, now: datetime
) -> ExecutionResult:
    """Execute one claim through the complete fail-closed lifecycle."""
    write_token = _WRITE_STARTED.set(False)
    if not _setting(settings, "meta_autonomy_enabled", False):
        _WRITE_STARTED.reset(write_token)
        return ExecutionResult(ExecutionStatus.REFUSED, "disabled")
    if _setting(settings, "meta_autonomy_shadow", True):
        _WRITE_STARTED.reset(write_token)
        return ExecutionResult(ExecutionStatus.REFUSED, "shadow_mode")
    if claim.campaign_id not in tuple(_setting(settings, "meta_autonomy_campaign_ids", ())):
        _WRITE_STARTED.reset(write_token)
        return ExecutionResult(ExecutionStatus.REFUSED, "not_allowlisted")
    if engine is None or not await begin_execution(engine, claim):
        _WRITE_STARTED.reset(write_token)
        return ExecutionResult(ExecutionStatus.REFUSED, "lease_lost")
    try:
        publication = await _publication(engine, claim.campaign_id)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            _WRITE_STARTED.reset(write_token)
            raise
        if _transient(exc):
            retry_at = now + timedelta(minutes=5)
            released = await release_action(
                engine,
                claim,
                failure_category="preflight_transient",
                next_evaluation_at=retry_at,
            )
            _WRITE_STARTED.reset(write_token)
            if released:
                return ExecutionResult(
                    ExecutionStatus.RETRYABLE,
                    "preflight_transient",
                    retry_at=retry_at,
                )
            return ExecutionResult(ExecutionStatus.FAILED, "lease_lost")
        _WRITE_STARTED.reset(write_token)
        raise
    if (
        publication is None
        or len(_ids(publication)) != 3
        or publication.get("state") not in {"active", "published"}
    ):
        finalized = await finish_action(
            engine, claim, status=ActionStatus.CANCELLED, failure_category="publication_changed"
        )
        if not finalized:
            _WRITE_STARTED.reset(write_token)
            return ExecutionResult(ExecutionStatus.FAILED, "lease_lost")
        _WRITE_STARTED.reset(write_token)
        return ExecutionResult(ExecutionStatus.CANCELLED, "publication_changed")
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(engine, claim, lost))
    before = None
    try:
        before = await _external(
            engine, claim, meta, "read_source", claim.campaign_id, ids=_ids(publication)
        )
        history = _history_events(await campaign_history(engine, claim.campaign_id))
        reason = _policy_reason(claim, settings, publication, before, history, now)
        if reason:
            finalized = await finish_action(
                engine,
                claim,
                status=ActionStatus.CANCELLED,
                before_state={"observed": before, "intent": _intent(claim)},
                failure_category=reason,
            )
            if not finalized:
                return ExecutionResult(ExecutionStatus.FAILED, "lease_lost", before_state=before)
            return ExecutionResult(ExecutionStatus.CANCELLED, reason, before_state=before)
        if claim.kind is DecisionKind.PAUSE:
            await _write_external(engine, claim, meta, "pause_source", source=before)
            after = await _external(
                engine, claim, meta, "read_source", claim.campaign_id, ids=_ids(publication)
            )
            hierarchy = after.get("hierarchy") or {}
            if (
                after.get("status") != "PAUSED"
                or after.get("effective_status") not in _PAUSED_EFFECTIVE
                or any(
                    hierarchy.get(key, {}).get("status") != "ACTIVE"
                    for key in ("campaign", "ad_set")
                )
            ):
                raise RuntimeError("pause_verification_failed")
            budget_event = None
        elif claim.kind is DecisionKind.SCALE:
            await _write_external(
                engine,
                claim,
                meta,
                "set_budget",
                ad_set_id=before["ad_set_id"],
                cents=claim.decision.new_budget_cents,
            )
            after = await _external(
                engine, claim, meta, "read_source", claim.campaign_id, ids=_ids(publication)
            )
            if after.get(
                "budget_cents", after.get("daily_budget")
            ) != claim.decision.new_budget_cents or not _source_ok(
                after,
                claim.campaign_id,
                _ids(publication),
                claim.decision.new_budget_cents,
            ):
                raise RuntimeError("budget_verification_failed")
            budget_event = (claim.decision.old_budget_cents, claim.decision.new_budget_cents)
        elif claim.kind is DecisionKind.REALLOCATE:
            allocation_after = {}
            rollback = {}
            allocation_writes = []
            try:
                for item in claim.decision.allocations.values():
                    observed = await _external(
                        engine, claim, meta, "read_allocation", allocation=item
                    )
                    if (
                        any(
                            observed.get(key) != item[key]
                            for key in ("campaign_id", "variant_id", "ad_set_id", "ad_id")
                        )
                        or item["campaign_id"] != claim.campaign_id
                        or observed.get("ad_set", {}).get("daily_budget")
                        != item["old_budget_cents"]
                        or observed.get("ad_set", {}).get("status") != "ACTIVE"
                        or observed.get("ad_set", {}).get("effective_status")
                        not in _ACTIVE_EFFECTIVE
                        or observed.get("ad", {}).get("status") != "ACTIVE"
                        or observed.get("ad", {}).get("effective_status") not in _ACTIVE_EFFECTIVE
                    ):
                        raise RuntimeError("reallocation_live_state_changed")
                for label in ("loser", "winner"):
                    item = claim.decision.allocations[label]
                    allocation_writes.append(label)
                    await _write_external(
                        engine,
                        claim,
                        meta,
                        "set_budget",
                        ad_set_id=item["ad_set_id"],
                        cents=item["new_budget_cents"],
                    )
                for label, item in claim.decision.allocations.items():
                    observed = await _external(
                        engine, claim, meta, "read_allocation", allocation=item
                    )
                    if (
                        any(
                            observed.get(key) != item[key]
                            for key in ("campaign_id", "variant_id", "ad_set_id", "ad_id")
                        )
                        or observed["ad_set"]["daily_budget"] != item["new_budget_cents"]
                        or observed["ad_set"].get("status") != "ACTIVE"
                        or observed["ad_set"].get("effective_status") not in _ACTIVE_EFFECTIVE
                        or observed["ad"].get("status") != "ACTIVE"
                        or observed["ad"].get("effective_status") not in _ACTIVE_EFFECTIVE
                    ):
                        raise RuntimeError("reallocation_verification_failed")
                    allocation_after[label] = observed
            except BaseException:
                for label in allocation_writes:
                    item = claim.decision.allocations[label]
                    try:
                        mutation = await _write_external(
                            engine,
                            claim,
                            meta,
                            "set_budget",
                            ad_set_id=item["ad_set_id"],
                            cents=item["old_budget_cents"],
                        )
                        verified = await _external(
                            engine, claim, meta, "read_allocation", allocation=item
                        )
                        if (
                            any(
                                verified.get(key) != item[key]
                                for key in ("campaign_id", "variant_id", "ad_set_id", "ad_id")
                            )
                            or verified.get("ad_set", {}).get("daily_budget")
                            != item["old_budget_cents"]
                            or verified.get("ad_set", {}).get("status") != "ACTIVE"
                            or verified.get("ad_set", {}).get("effective_status")
                            not in _ACTIVE_EFFECTIVE
                            or verified.get("ad", {}).get("status") != "ACTIVE"
                            or verified.get("ad", {}).get("effective_status")
                            not in _ACTIVE_EFFECTIVE
                        ):
                            raise RuntimeError("reallocation_rollback_unproven")
                        rollback[label] = {"mutation": mutation, "verified": verified}
                    except Exception as rollback_error:
                        rollback[label] = {"error": type(rollback_error).__name__}
                raise
            after = {"source": before, "allocations": allocation_after}
            budget_event = (claim.decision.old_budget_cents, claim.decision.new_budget_cents)
        elif claim.kind is DecisionKind.REPLACE:
            after, rollback = await _replace(
                engine, settings, meta, replacement_builder, claim, before
            )
            budget_event = None
        else:
            after, budget_event = before, None
        if lost.is_set():
            raise RuntimeError("execution lease ownership was lost")
        if not await finish_action(
            engine,
            claim,
            status=ActionStatus.SUCCEEDED,
            before_state={"observed": before, "intent": _intent(claim)},
            after_state=after,
            rollback_result=locals().get("rollback"),
            budget=budget_event,
        ):
            return ExecutionResult(ExecutionStatus.FAILED, "lease_lost", before, after)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, "executed", before, after)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not _WRITE_STARTED.get() and _transient(exc):
            retry_at = now + timedelta(minutes=5)
            released = await release_action(
                engine, claim, failure_category="meta_rate_limit", next_evaluation_at=retry_at
            )
            if not released:
                return ExecutionResult(ExecutionStatus.FAILED, "lease_lost", before)
            return ExecutionResult(
                ExecutionStatus.RETRYABLE, "meta_rate_limit", before, retry_at=retry_at
            )
        reconciled = await block_campaign_for_reconciliation(
            engine,
            claim,
            before_state={"observed": before, "intent": _intent(claim)},
            rollback_result=(
                exc.rollback_result if isinstance(exc, _SagaFailure) else locals().get("rollback")
            ),
            failure_category="external_state_unproven",
            failure_message=type(exc).__name__,
        )
        if reconciled:
            return ExecutionResult(
                ExecutionStatus.RECONCILIATION_REQUIRED, "external_state_unproven", before
            )
        return ExecutionResult(ExecutionStatus.FAILED, "lease_lost", before)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        _WRITE_STARTED.reset(write_token)
