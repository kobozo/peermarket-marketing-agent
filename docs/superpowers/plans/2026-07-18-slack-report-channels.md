# Per-platform Slack report channels + Block Kit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route report messages (autonomy audits, daily performance summary, hourly Meta alerts) to per-platform Slack channels and render them as Block Kit messages instead of flat JSON text.

**Architecture:** A pure routing helper maps the domain `channel` value (`tiktok`/`meta`/`email`) to an env-configured Slack channel ID with founder-DM fallback. Pure block-builder functions live in a new `slack_blocks.py`; blocks are rendered at enqueue time for the durable `slack_outbox` (stored in the JSONB payload next to the fallback `text`) and at send time for the direct senders. `SlackNotifier` forwards an optional `blocks` list to `chat_postMessage`.

**Tech Stack:** Python 3.12, pydantic-settings, slack_sdk `AsyncWebClient`, SQLAlchemy async + Postgres, pytest.

Spec: `docs/superpowers/specs/2026-07-18-slack-report-channels-design.md`

## Global Constraints

- Never break the founder-DM fallback: unset routing env vars must reproduce today's behaviour exactly.
- `text` is always still sent alongside `blocks` (Slack notification fallback requirement).
- No DB schema migration: use the existing `slack_outbox.channel_id` column and JSONB payload.
- Block builders are pure functions (no I/O, no settings access), tolerant of missing/None fields — a builder must never raise on a sparse payload.
- Slack limits respected: header text ≤150 chars, section text ≤3000 chars, section fields ≤10, ≤50 blocks per message.
- Run commands with `export PATH=$HOME/.local/node22/bin:$PATH` not needed for Python; use `uv run pytest`.
- Match existing code style (structlog, `sql_text`, frozen dataclasses, no needless comments).
- Do NOT run `git commit` inside parallel tasks 1–3 (shared worktree); the orchestrator commits. Tasks 4–5 run sequentially and may commit.

---

### Task 1: Routing config + helper  [model: sonnet]

**Files:**
- Modify: `src/peermarket_agent/config.py` (Slack section, after line 23)
- Modify: `.env.example` (Slack section — read the file first, append the three vars there with the real IDs as comments)
- Create: `src/peermarket_agent/slack_routing.py`
- Test: `tests/test_slack_routing.py`

**Interfaces:**
- Produces: `report_channel_id(settings, channel: str) -> str | None` — returns the configured Slack channel ID for domain channel `"tiktok"|"meta"|"email"`, else `None` (unknown channel or unset setting). Settings fields: `slack_report_channel_tiktok`, `slack_report_channel_meta`, `slack_report_channel_email` (all `str = ""`).

- [ ] **Step 1: Write the failing test** (read `tests/test_slack_routing.py` doesn't exist; look at one existing test for style, e.g. `tests/test_slack_notifier.py`)

```python
"""Routing of domain channels to configured Slack report channels."""

from types import SimpleNamespace

from peermarket_agent.slack_routing import report_channel_id


def _settings(**overrides) -> SimpleNamespace:
    defaults = {
        "slack_report_channel_tiktok": "",
        "slack_report_channel_meta": "",
        "slack_report_channel_email": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_maps_each_domain_channel_to_its_configured_id() -> None:
    settings = _settings(
        slack_report_channel_tiktok="C0BJ71Z4YFL",
        slack_report_channel_meta="C0BJ0PUURRR",
        slack_report_channel_email="C0BHRLPM3QX",
    )
    assert report_channel_id(settings, "tiktok") == "C0BJ71Z4YFL"
    assert report_channel_id(settings, "meta") == "C0BJ0PUURRR"
    assert report_channel_id(settings, "email") == "C0BHRLPM3QX"


def test_unset_setting_falls_back_to_none() -> None:
    assert report_channel_id(_settings(), "meta") is None


def test_unknown_channel_falls_back_to_none() -> None:
    settings = _settings(slack_report_channel_meta="C0BJ0PUURRR")
    assert report_channel_id(settings, "seo_pr") is None
    assert report_channel_id(settings, "") is None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_slack_routing.py -v` → FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `src/peermarket_agent/slack_routing.py`**

```python
"""Map domain channels to configured Slack report channels."""

_CHANNEL_SETTING_FIELDS = {
    "tiktok": "slack_report_channel_tiktok",
    "meta": "slack_report_channel_meta",
    "email": "slack_report_channel_email",
}


def report_channel_id(settings, channel: str) -> str | None:
    """Slack channel ID for a domain channel; None means fall back to the founder DM."""
    field = _CHANNEL_SETTING_FIELDS.get(channel)
    if field is None:
        return None
    return getattr(settings, field, "") or None
```

- [ ] **Step 4: Add settings fields** in `config.py` directly under `slack_founder_user_id` (line 23):

```python
    # Per-platform report channels (empty falls back to the founder DM)
    slack_report_channel_tiktok: str = ""
    slack_report_channel_meta: str = ""
    slack_report_channel_email: str = ""
```

- [ ] **Step 5: Add to `.env.example`** in its Slack section (keep the file's existing comment style):

```
# Per-platform report channels (optional; empty = founder DM)
SLACK_REPORT_CHANNEL_TIKTOK=C0BJ71Z4YFL
SLACK_REPORT_CHANNEL_META=C0BJ0PUURRR
SLACK_REPORT_CHANNEL_EMAIL=C0BHRLPM3QX
```

- [ ] **Step 6: Run** `uv run pytest tests/test_slack_routing.py -v` → PASS. Also `uv run pytest tests/test_config.py -v` if it exists → PASS.

---

### Task 2: `SlackNotifier` blocks passthrough  [model: sonnet]

**Files:**
- Modify: `src/peermarket_agent/slack_notifier.py:22-77`
- Test: `tests/test_slack_notifier.py` (read it first, extend in style)

**Interfaces:**
- Produces: `SlackNotifier.send_message(text, *, channel_id=None, thread_ts=None, blocks: list[dict] | None = None)` and `SlackNotifier.notify_founder(text, *, blocks: list[dict] | None = None)`. When `blocks` is falsy it must NOT appear in the `chat_postMessage` kwargs.

- [ ] **Step 1: Write failing tests** (append; adapt fixture/mocking style from the existing file — it likely stubs `AsyncWebClient.chat_postMessage`):

```python
async def test_send_message_forwards_blocks() -> None:
    notifier, recorded = _notifier_with_recorder()  # reuse/build the file's existing helper pattern
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    await notifier.send_message("fallback", channel_id="C123", blocks=blocks)
    assert recorded["blocks"] == blocks
    assert recorded["text"] == "fallback"


async def test_send_message_omits_blocks_kwarg_when_absent() -> None:
    notifier, recorded = _notifier_with_recorder()
    await notifier.send_message("plain", channel_id="C123")
    assert "blocks" not in recorded


async def test_notify_founder_forwards_blocks() -> None:
    notifier, recorded = _notifier_with_recorder()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    await notifier.notify_founder("fallback", blocks=blocks)
    assert recorded["blocks"] == blocks
```

- [ ] **Step 2: Run** `uv run pytest tests/test_slack_notifier.py -v` → new tests FAIL (unexpected keyword argument)

- [ ] **Step 3: Implement.** In `send_message`, add the parameter and:

```python
        kwargs = {"channel": channel, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
```

In `notify_founder`, add `*, blocks: list[dict] | None = None` and build the same conditional kwargs for its `chat_postMessage` call.

- [ ] **Step 4: Run** `uv run pytest tests/test_slack_notifier.py -v` → PASS

---

### Task 3: Block builders (`slack_blocks.py`)  [model: opus]

**Files:**
- Create: `src/peermarket_agent/slack_blocks.py`
- Test: `tests/test_slack_blocks.py`

**Interfaces:**
- Produces (all pure, all return `list[dict]`, all tolerate missing/None fields):
  - `autonomy_audit_blocks(payload: dict) -> list[dict]` — consumes the `_audit` payload dict (keys: `outcome`, `decision`, `campaign_id`, `reason`, `experiment_id`, `detail`, `evidence` [list of variant sample dicts], `thresholds`, `budgets` {`previous_cents`,`new_cents`}, `rollback`, `affected_ads`, `next_evaluation_at`, `replacement_result`).
  - `daily_summary_blocks(message: str) -> list[dict]` — consumes the pre-rendered `_summary` text (first line = title, then `• Publication #N — a; b; c` lines).
  - `hourly_alert_blocks(message: str) -> list[dict]`.

- [ ] **Step 1: Write failing tests** using the real sample payload:

```python
"""Block Kit builders render structured reports without raw JSON dumps."""

import json

from peermarket_agent.slack_blocks import (
    autonomy_audit_blocks,
    daily_summary_blocks,
    hourly_alert_blocks,
)

_AUDIT_PAYLOAD = {
    "audit": "autonomy",
    "campaign_id": "120249125021520342",
    "outcome": "shadow",
    "decision": "observe",
    "reason": "not_comparable",
    "experiment_id": None,
    "detail": "decision persisted; no action queued",
    "thresholds": {
        "cooldown_hours": 24,
        "min_impressions": 1000,
        "min_landing_page_views": 30,
        "min_registrations": 10,
    },
    "evidence": [
        {"variant_id": "156", "impressions": 11303, "landing_page_views": 42, "registrations": 0}
    ],
    "affected_ads": [
        {"publication_id": 1, "ad_set_id": "120249125021910342", "ad_id": "120249125024900342"}
    ],
    "budgets": {"previous_cents": None, "new_cents": None},
    "rollback": {"needed": False, "result": "not_required"},
    "next_evaluation_at": "2026-07-18T22:00:00+00:00",
    "replacement_result": None,
}


def test_autonomy_blocks_start_with_header_and_contain_no_json_dumps() -> None:
    blocks = autonomy_audit_blocks(_AUDIT_PAYLOAD)
    assert blocks[0]["type"] == "header"
    assert "shadow" in blocks[0]["text"]["text"]
    rendered = json.dumps(blocks)
    assert "{'" not in rendered and '{\\"' not in rendered  # no python/json dict dumps in copy
    assert "11,303" in rendered  # formatted numbers
    assert "not_comparable" in rendered
    assert "2026-07-18" in rendered


def test_autonomy_blocks_show_variant_metrics_as_fields() -> None:
    blocks = autonomy_audit_blocks(_AUDIT_PAYLOAD)
    fields_sections = [b for b in blocks if b.get("fields")]
    assert fields_sections, "expected a fields section for variant samples"
    field_text = fields_sections[0]["fields"][0]["text"]
    assert "156" in field_text and "42" in field_text


def test_autonomy_blocks_survive_sparse_payload() -> None:
    assert autonomy_audit_blocks({})[0]["type"] == "header"


def test_daily_summary_blocks_split_title_and_publications() -> None:
    message = (
        "Daily campaign evidence summary (descriptive observations only)\n"
        "• Publication #7 — approved budget 2000 cents; spend 150 cents; impressions 11303"
    )
    blocks = daily_summary_blocks(message)
    assert blocks[0]["type"] == "header"
    body = blocks[1]["text"]["text"]
    assert "Publication #7" in body
    assert "; " not in body  # semicolon runs become newlines


def test_hourly_alert_blocks_wrap_message() -> None:
    blocks = hourly_alert_blocks("Draft #3: Meta delivery problem: no_delivery")
    assert blocks[0]["type"] == "section"
    assert "no_delivery" in blocks[0]["text"]["text"]
    assert "⚠️" in blocks[0]["text"]["text"]
    recovered = hourly_alert_blocks("Meta delivery recovered from no_delivery")
    assert "✅" in recovered[0]["text"]["text"]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_slack_blocks.py -v` → FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `src/peermarket_agent/slack_blocks.py`:**

```python
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
        locales = ", ".join(sorted((replacement.get("ad_ids") or {})))
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
```

- [ ] **Step 4: Run** `uv run pytest tests/test_slack_blocks.py -v` → PASS

---

### Task 4: Outbox carries blocks + autonomy audit routing  [model: opus]

**Files:**
- Modify: `src/peermarket_agent/slack_outbox.py:16-24` (OutboxMessage), `:92-96` (deliver), `:165-175` (claim mapping)
- Modify: `src/peermarket_agent/agent/loops/autonomy.py:525-652` (`_audit`) and every `_audit(` call site (grep the file; pass `settings=`)
- Test: `tests/test_slack_outbox.py`, `tests/test_autonomy_loop.py` (read both first; extend in style)

**Interfaces:**
- Consumes: `report_channel_id` (Task 1), `autonomy_audit_blocks` (Task 3).
- Produces: `OutboxMessage` gains `blocks: list | None = None`; `_audit` gains keyword `settings: Any = None`; audit payload gains `"detail"` and `"blocks"` keys; audit outbox rows get `channel_id` populated when routing is configured.

- [ ] **Step 1: Failing tests.** In `tests/test_slack_outbox.py` add (adapting to the file's existing enqueue/deliver fixtures): a test that a payload containing `"blocks"` is delivered with `send_message(..., blocks=<those blocks>)`, and a legacy payload without `"blocks"` delivers with `blocks=None`. In `tests/test_autonomy_loop.py` add: after an audited cycle with settings where `slack_report_channel_meta="C0BJ0PUURRR"`, the inserted `slack_outbox` row has `channel_id='C0BJ0PUURRR'` and `payload['blocks'][0]['type'] == 'header'` and `payload['detail']` set; with the setting empty, `channel_id IS NULL`.

- [ ] **Step 2: Run** the two test files → new tests FAIL.

- [ ] **Step 3: Implement outbox side.**

```python
@dataclass(frozen=True)
class OutboxMessage:
    id: int
    draft_id: int
    channel_id: str | None
    root_ts: str | None
    message_kind: str
    text: str
    blocks: list | None = None
```

In `_claim_pending_outbox` mapping: `blocks=row["payload"].get("blocks") or None,`
In `deliver_pending_outbox`: `result = await notifier.send_message(row.text, channel_id=row.channel_id, thread_ts=row.root_ts, blocks=row.blocks)`

- [ ] **Step 4: Implement autonomy side.** `_audit` signature gains `settings: Any = None`. After the `payload = {...}` literal (keep `"text"` unchanged), add `"detail": detail,` inside the literal, then:

```python
    payload["blocks"] = autonomy_audit_blocks(payload)
    channel_id = report_channel_id(settings, "meta") if settings is not None else None
```

Change the INSERT to include the column:

```sql
INSERT INTO slack_outbox(idempotency_key,draft_id,channel_id,message_kind,payload,autonomy_campaign_id)
VALUES (:key,:draft,:channel,'autonomy_audit',CAST(:payload AS JSONB),:campaign)
ON CONFLICT (idempotency_key) DO NOTHING
```

with `"channel": channel_id` in the params. Imports: `from peermarket_agent.slack_blocks import autonomy_audit_blocks` and `from peermarket_agent.slack_routing import report_channel_id`. Update every `_audit(` call site in the file to pass `settings=settings` (they all live inside `run_autonomy_cycle`/helpers that receive settings — thread it through where needed).

- [ ] **Step 5: Run** `uv run pytest tests/test_slack_outbox.py tests/test_autonomy_loop.py -v` → PASS

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: route autonomy audits to report channel with Block Kit payload"`

---

### Task 5: Daily summary + hourly alert routing/styling  [model: opus]

**Files:**
- Modify: `src/peermarket_agent/agent/loops/performance_daily.py:470-491` (`_drain_summaries`) and its call site in `run_daily_performance`
- Modify: `src/peermarket_agent/agent/loops/hourly.py:88-95` (`_deliver`), `_send_claimed_alert`, `_send_attribution_availability_alert`, `collect_meta_performance` (thread `channel_id`)
- Test: `tests/test_performance_daily.py`, `tests/test_hourly*.py` (read first, extend in style)

**Interfaces:**
- Consumes: `report_channel_id` (Task 1), `daily_summary_blocks`, `hourly_alert_blocks` (Task 3), notifier `blocks` kwargs (Task 2).
- Produces: `_drain_summaries(engine, notifier, now, *, channel_id: str | None = None)`; `_deliver(notifier, message, *, channel_id=None, blocks=None)`.

- [ ] **Step 1: Failing tests.** Daily: with `slack_report_channel_meta` set, the summary is sent via `send_message` with `channel_id="C0BJ0PUURRR"` and blocks whose first block is a header; with it unset, behaviour matches today (founder delivery still succeeds). Hourly: a claimed alert with routing set calls `send_message(channel_id=..., blocks=[section])`; with routing unset it still calls `notify_founder`.

- [ ] **Step 2: Run** the two test files → new tests FAIL.

- [ ] **Step 3: Implement daily.** `_drain_summaries` gains `*, channel_id: str | None = None`; replace the `notify_founder` call:

```python
        try:
            await notifier.send_message(
                message, channel_id=channel_id, blocks=daily_summary_blocks(message)
            )
            delivered = True
        except Exception:
            delivered = False
            failure = "notification_exception"
```

(`send_message` falls back to the founder DM when `channel_id` is None; it raises when neither is configured, which lands in the existing failure path — keep `failure = "notification_not_confirmed"` logic removed since `send_message` never returns falsy; delete the unreachable branch.) Call site in `run_daily_performance`: `await _drain_summaries(engine, notifier, now, channel_id=report_channel_id(settings, "meta"))`.

- [ ] **Step 4: Implement hourly.**

```python
async def _deliver(notifier, message: str, *, channel_id: str | None = None, blocks=None) -> bool:
    if notifier is None:
        return False
    try:
        if channel_id:
            await notifier.send_message(message, channel_id=channel_id, blocks=blocks)
            return True
        return bool(await notifier.notify_founder(message, blocks=blocks))
    except Exception:
        log.warning("hourly_meta.alert_failed")
        return False
```

Thread `channel_id` from `collect_meta_performance` (compute once: `report_channel = report_channel_id(settings, "meta")`) into `_send_claimed_alert(..., channel_id=report_channel)` and `_send_attribution_availability_alert(..., channel_id=report_channel)`; both pass `_deliver(notifier, message, channel_id=channel_id, blocks=hourly_alert_blocks(message))`.

- [ ] **Step 5: Run** `uv run pytest tests/test_performance_daily.py tests/test_hourly*.py -v` → PASS (glob: run whatever hourly test files exist)

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: route daily/hourly Meta reports to report channel with blocks"`

---

### Task 6: Full verification

- [ ] `uv run pytest` — full suite green (DB-backed tests need the local docker pgvector Postgres per README; start it if needed).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` if ruff is configured (check pyproject).
- [ ] Push branch, open PR, watch CI to green, merge (explicitly authorized).
- [ ] Live rights check with the real bot token: `auth.test`, then post one styled test message to each of `C0BJ71Z4YFL`, `C0BJ0PUURRR`, `C0BHRLPM3QX`; report `not_in_channel`/`missing_scope` errors as required right changes.
