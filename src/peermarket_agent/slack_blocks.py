"""Slack Block Kit builders for report messages (pure functions, no I/O)."""

_MAX_BLOCKS = 50
_MAX_SECTION_FIELDS = 10


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def _count(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "–"


def _euros(cents) -> str:
    try:
        return f"€{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return "–"


def autonomy_audit_blocks(payload: dict) -> list[dict]:
    outcome = payload.get("outcome") or "audit"
    decision = payload.get("decision") or "unknown"
    blocks = [_header(f"🤖 Autonomy {outcome} — {decision}")]

    overview = [
        f"*Campaign:* `{payload.get('campaign_id') or 'unknown'}`",
        f"*Reason:* {payload.get('reason') or 'unknown'}",
    ]
    if payload.get("experiment_id"):
        overview.append(f"*Experiment:* {payload['experiment_id']}")
    if payload.get("detail"):
        overview.append(f"*Detail:* {payload['detail']}")
    blocks.append(_section("\n".join(overview)))

    fields = [
        {
            "type": "mrkdwn",
            "text": (
                f"*Variant {sample.get('variant_id') or '?'}*\n"
                f"{_count(sample.get('impressions'))} impressions\n"
                f"{_count(sample.get('landing_page_views'))} landing-page views\n"
                f"{_count(sample.get('registrations'))} registrations"
            ),
        }
        for sample in payload.get("evidence") or []
    ]
    if fields:
        blocks.append({"type": "section", "fields": fields[:_MAX_SECTION_FIELDS]})

    budgets = payload.get("budgets") or {}
    previous, new = budgets.get("previous_cents"), budgets.get("new_cents")
    budget_line = (
        "unchanged" if previous is None and new is None else f"{_euros(previous)} → {_euros(new)}"
    )
    rollback = payload.get("rollback") or {}
    rollback_line = (
        str(rollback.get("result") or "unknown") if rollback.get("needed") else "not required"
    )
    ads = payload.get("affected_ads") or []
    ad_ids = ", ".join(f"`{item.get('ad_id')}`" for item in ads[:5] if item.get("ad_id"))
    state = [
        f"*Budget:* {budget_line}",
        f"*Rollback:* {rollback_line}",
        f"*Affected ads:* {len(ads)}" + (f" ({ad_ids})" if ad_ids else ""),
    ]
    replacement = payload.get("replacement_result")
    if isinstance(replacement, dict):
        locales = ", ".join(sorted(replacement.get("ad_ids") or {}))
        state.append(
            f"*Replacement:* new ads ({locales}) in ad set "
            f"`{replacement.get('ad_set_id')}`; source `{replacement.get('source_ad_id')}` "
            f"{replacement.get('source_status') or 'paused'}"
        )
    blocks.append(_section("\n".join(state)))

    thresholds = payload.get("thresholds") or {}
    footer = (
        f"Thresholds: ≥{_count(thresholds.get('min_impressions'))} impressions · "
        f"≥{_count(thresholds.get('min_landing_page_views'))} LPV · "
        f"≥{_count(thresholds.get('min_registrations'))} registrations · "
        f"cooldown {thresholds.get('cooldown_hours', '–')}h"
        f"\nNext evaluation: {payload.get('next_evaluation_at') or 'pending'}"
    )
    blocks.append(_context(footer))
    return blocks[:_MAX_BLOCKS]


def daily_summary_blocks(message: str) -> list[dict]:
    lines = [line for line in message.splitlines() if line.strip()]
    if not lines:
        return [_section(message or " ")]
    blocks = [_header(f"📊 {lines[0]}")]
    body_lines = lines[1:]
    for line in body_lines[: _MAX_BLOCKS - 2]:
        stripped = line.lstrip("• ").strip()
        title, separator, rest = stripped.partition(" — ")
        if separator:
            blocks.append(_section(f"*{title}*\n" + rest.replace("; ", "\n")))
        else:
            blocks.append(_section(stripped.replace("; ", "\n")))
    hidden = len(body_lines) - (_MAX_BLOCKS - 2)
    if hidden > 0:
        blocks.append(_context(f"…and {hidden} more publications"))
    return blocks[:_MAX_BLOCKS]


def hourly_alert_blocks(message: str) -> list[dict]:
    emoji = "✅" if "recovered" in message.lower() else "⚠️"
    return [_section(f"{emoji} *Meta delivery*\n{message}")]
