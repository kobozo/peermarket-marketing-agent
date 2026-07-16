"""Read-only collection and normalization of Meta ad delivery insights."""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from facebook_business.adobjects.ad import Ad
from facebook_business.api import FacebookAdsApi

from peermarket_agent.meta_ads import MetaConfig

_FIELDS = [
    "spend",
    "impressions",
    "reach",
    "clicks",
    "inline_link_clicks",
    "outbound_clicks",
    "actions",
    "ctr",
    "cpc",
    "cpm",
    "frequency",
]
_RATE_LIMIT_CODES = {4, 17, 32, 341, 613, 80004}
_TRANSIENT_CODES = {1, 2}
_MAX_ATTEMPTS = 3
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class MetaInsightSnapshot:
    ad_id: str
    window_start: date
    window_stop: date
    retrieved_at: datetime
    spend_cents: int
    impressions: int
    reach: int
    clicks: int
    inline_link_clicks: int
    outbound_clicks: int
    landing_page_views: int
    ctr: Decimal | None
    cpc_cents: int | None
    cpm_cents: int | None
    frequency: Decimal | None
    actions: dict[str, int]


class MetaInsightsError(RuntimeError):
    """Sanitized Meta Insights failure safe for persistence and logs."""

    def __init__(self, message: str, *, transient: bool) -> None:
        self.transient = transient
        super().__init__(message)


def _call_error_attribute(error: Exception, name: str) -> Any:
    value = getattr(error, name, None)
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _error_details(error: Exception) -> tuple[int | None, str | None, int | None]:
    code = _call_error_attribute(error, "api_error_code")
    error_type = _call_error_attribute(error, "api_error_type")
    status = _call_error_attribute(error, "http_status")
    return (
        code if isinstance(code, int) else None,
        error_type if isinstance(error_type, str) else None,
        status if isinstance(status, int) else None,
    )


def _is_transient(error: Exception) -> bool:
    explicitly_transient = _call_error_attribute(error, "api_transient_error")
    code, _, status = _error_details(error)
    return bool(
        explicitly_transient
        or code in _RATE_LIMIT_CODES
        or code in _TRANSIENT_CODES
        or status == 429
        or (status is not None and status >= 500)
    )


def _sanitized_error(error: Exception, *, transient: bool) -> MetaInsightsError:
    code, error_type, status = _error_details(error)
    return MetaInsightsError(
        f"Meta Insights request failed (code={code}, type={error_type}, status={status})",
        transient=transient,
    )


def _decimal(value: object, *, default: Decimal = Decimal(0)) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Meta Insights returned an invalid numeric value") from error


def _integer(value: object) -> int:
    return int(_decimal(value))


def _cents(euros: Decimal) -> int:
    return int((euros / _CENT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _action_values(value: object) -> dict[str, int]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return {}
    totals: dict[str, int] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        action_type = item.get("action_type")
        if not isinstance(action_type, str) or not action_type:
            continue
        totals[action_type] = totals.get(action_type, 0) + _integer(item.get("value"))
    return totals


def _outbound_clicks(value: object) -> int:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return sum(_action_values(value).values())
    return _integer(value)


def _fetch_rows(
    config: MetaConfig,
    ad_id: str,
    start: date,
    stop: date,
) -> list[Mapping[str, object]]:
    FacebookAdsApi.init(
        app_id=config.app_id,
        app_secret=config.app_secret,
        access_token=config.system_user_token,
    )
    cursor = Ad(ad_id).get_insights(
        fields=_FIELDS,
        params={
            "time_range": {"since": start.isoformat(), "until": stop.isoformat()},
            "time_increment": 1,
        },
    )
    return list(cursor)


def _normalize(
    rows: Iterable[Mapping[str, object]],
    *,
    ad_id: str,
    start: date,
    stop: date,
) -> MetaInsightSnapshot:
    spend = Decimal(0)
    impressions = reach = clicks = inline_link_clicks = outbound_clicks = 0
    actions: dict[str, int] = {}
    for row in rows:
        spend += _decimal(row.get("spend"))
        impressions += _integer(row.get("impressions"))
        reach += _integer(row.get("reach"))
        clicks += _integer(row.get("clicks"))
        inline_link_clicks += _integer(row.get("inline_link_clicks"))
        outbound_clicks += _outbound_clicks(row.get("outbound_clicks"))
        for action_type, count in _action_values(row.get("actions")).items():
            actions[action_type] = actions.get(action_type, 0) + count

    spend_cents = _cents(spend)
    return MetaInsightSnapshot(
        ad_id=ad_id,
        window_start=start,
        window_stop=stop,
        retrieved_at=datetime.now(UTC),
        spend_cents=spend_cents,
        impressions=impressions,
        reach=reach,
        clicks=clicks,
        inline_link_clicks=inline_link_clicks,
        outbound_clicks=outbound_clicks,
        landing_page_views=actions.get("landing_page_view", 0),
        ctr=(Decimal(clicks) * 100 / Decimal(impressions)) if impressions else None,
        cpc_cents=(
            int((Decimal(spend_cents) / Decimal(clicks)).quantize(Decimal(1), ROUND_HALF_UP))
            if clicks
            else None
        ),
        cpm_cents=(
            int(
                (Decimal(spend_cents) * 1000 / Decimal(impressions)).quantize(
                    Decimal(1), ROUND_HALF_UP
                )
            )
            if impressions
            else None
        ),
        frequency=(Decimal(impressions) / Decimal(reach)) if reach else None,
        actions=actions,
    )


async def fetch_meta_insights(
    config: MetaConfig,
    ad_id: str,
    start: date,
    stop: date,
    max_attempts: int = _MAX_ATTEMPTS,
) -> MetaInsightSnapshot:
    """Fetch an ad's exact date window without changing any Meta resource."""
    if not 1 <= max_attempts <= _MAX_ATTEMPTS:
        raise ValueError("max_attempts must be between 1 and 3")

    for attempt in range(1, max_attempts + 1):
        try:
            rows = await asyncio.to_thread(_fetch_rows, config, ad_id, start, stop)
            return _normalize(rows, ad_id=ad_id, start=start, stop=stop)
        except Exception as error:
            if isinstance(error, MetaInsightsError):
                raise
            transient = _is_transient(error)
            if not transient or attempt == max_attempts:
                raise _sanitized_error(error, transient=transient) from None
            await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))

    raise AssertionError("unreachable")
