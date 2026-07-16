# Jarvis Meta Monitoring and Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Jarvis ingest Meta delivery metrics, join consented PeerMarket funnel aggregates, report problems/results, and create only sufficiently evidenced learnings.

**Architecture:** A pure URL builder tags new ads; a read-only Insights client normalizes Meta data; a persistence/evaluation module updates `publications.performance`; the hourly loop collects delivery and aggregates while a daily loop summarizes evidence. Existing Slack approval remains the only path to paid mutations.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy/asyncpg, Meta Business SDK, PostgreSQL/JSONB, pytest, systemd, GitHub Actions.

## Global Constraints

- PeerMarket attribution must be deployed and production-verified before its Jarvis flag is enabled.
- Draft 156 remains an unattributed baseline; never rewrite its destination or Meta objects.
- Never pause, activate, replace, retarget, or change budgets from monitoring or learning code.
- A single advertisement creates observations only, never a reusable learning.
- Defaults: 3-day Insights lookback, 2-hour no-delivery grace, 1,000 impressions, 30 landing-page views, and 10 registrations per reusable-learning variant.
- Existing Meta and PeerMarket credentials remain secrets; all new controls are GitHub repository variables.
- Deploy only through a reviewed PR and the existing GitHub Actions workflow.

---

### Task 1: Stable tagged destination URLs

**Files:**
- Create: `src/peermarket_agent/campaign_urls.py`
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Create: `tests/test_campaign_urls.py`
- Modify: `tests/test_meta_pipeline.py`

**Interfaces:**
- Produces: `build_campaign_url(base_url: str, draft_id: int) -> str`.
- URL fields are exactly `facebook`, `paid_social`, `peermarket`, and `draft-<id>`.

- [ ] **Step 1: Write failing URL tests**

```python
def test_build_campaign_url_preserves_existing_query():
    assert build_campaign_url("https://peermarket.eu/?lang=nl", 200) == (
        "https://peermarket.eu/?lang=nl&utm_source=facebook&utm_medium=paid_social"
        "&utm_campaign=peermarket&utm_content=draft-200"
    )

@pytest.mark.parametrize("url", ["http://peermarket.eu/", "https://example.com/"])
def test_build_campaign_url_rejects_unsafe_destination(url):
    with pytest.raises(ValueError):
        build_campaign_url(url, 200)
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_campaign_urls.py tests/test_meta_pipeline.py`

Expected: builder does not exist and pipeline still passes the untagged root URL.

- [ ] **Step 3: Implement and wire the pure builder**

```python
def build_campaign_url(base_url: str, draft_id: int) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.hostname not in {"peermarket.eu", "www.peermarket.eu"}:
        raise ValueError("campaign destination must be HTTPS PeerMarket")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        utm_source="facebook", utm_medium="paid_social",
        utm_campaign="peermarket", utm_content=f"draft-{draft_id}",
    )
    return urlunsplit(parsed._replace(query=urlencode(query)))
```

Use it only for newly created Meta resources. Reconciliation and published replacement paths keep the frozen destination implied by their existing creative.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_campaign_urls.py tests/test_meta_pipeline.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/campaign_urls.py src/peermarket_agent/meta_pipeline.py tests/test_campaign_urls.py tests/test_meta_pipeline.py
git commit -m "feat: tag new Meta campaign destinations"
```

### Task 2: Read-only Meta Insights client

**Files:**
- Create: `src/peermarket_agent/meta_insights.py`
- Create: `tests/test_meta_insights.py`

**Interfaces:**
- Produces: immutable `MetaInsightSnapshot`.
- Produces: `async fetch_meta_insights(config, ad_id, start, stop, max_attempts=3) -> MetaInsightSnapshot`.
- Raises: sanitized `MetaInsightsError(transient: bool)`.

- [ ] **Step 1: Write failing normalization and retry tests**

```python
async def test_fetch_normalizes_missing_actions_and_decimals(meta_api):
    meta_api.rows = [{"spend": "2.17", "impressions": "1062", "actions": []}]
    snapshot = await fetch_meta_insights(CONFIG, "ad-1", date(2026, 7, 14), date(2026, 7, 16))
    assert snapshot.spend_cents == 217
    assert snapshot.impressions == 1062
    assert snapshot.landing_page_views == 0

async def test_transient_failure_retries_at_most_three_times(meta_api):
    meta_api.fail_transiently(times=2)
    await fetch_meta_insights(CONFIG, "ad-1", START, STOP, max_attempts=3)
    assert meta_api.calls == 3
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_meta_insights.py`

Expected: module is absent.

- [ ] **Step 3: Implement the read-only adapter**

```python
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
```

Call `Ad(ad_id).get_insights` in `asyncio.to_thread`, request the exact spec fields, sum paginated rows, parse action arrays by `action_type`, use decimal-safe cent conversion, retry only rate-limit/transient errors with bounded backoff, and redact credentials from all raised messages.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_meta_insights.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/meta_insights.py tests/test_meta_insights.py
git commit -m "feat: collect normalized Meta Insights"
```

### Task 3: Performance snapshots and delivery classification

**Files:**
- Create: `src/peermarket_agent/performance.py`
- Modify: `src/peermarket_agent/publications.py`
- Modify: `src/peermarket_agent/db/migrations.py`
- Create: `tests/test_performance.py`
- Modify: `tests/test_publications.py`

**Interfaces:**
- Produces: `derive_performance(previous, current) -> dict`.
- Produces: `classify_delivery(statuses, snapshot, published_at, now, grace_hours) -> str`.
- Produces: `save_performance_snapshot(engine, draft_id, payload) -> None` with row lock and JSONB update.

- [ ] **Step 1: Write failing delta, restatement, and classification tests**

```python
def test_meta_restatement_never_creates_negative_delta():
    result = derive_performance({"spend_cents": 300, "impressions": 1000}, {"spend_cents": 280, "impressions": 990})
    assert result["delta"] == {"spend_cents": 0, "impressions": 0}
    assert result["restated"] is True

def test_active_zero_impressions_after_grace_is_no_delivery():
    assert classify_delivery(ACTIVE, ZERO_SNAPSHOT, PUBLISHED_3H_AGO, NOW, 2) == "no_delivery"

def test_active_impressions_is_healthy():
    assert classify_delivery(ACTIVE, SNAPSHOT_WITH_IMPRESSIONS, PUBLISHED_3H_AGO, NOW, 2) == "healthy"
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_performance.py tests/test_publications.py`

Expected: performance functions are absent.

- [ ] **Step 3: Implement deterministic evaluation and atomic JSON update**

```python
def derive_performance(previous: dict | None, current: dict) -> dict:
    previous = previous or {}
    numeric = ("spend_cents", "impressions", "reach", "clicks", "inline_link_clicks", "outbound_clicks", "landing_page_views")
    restated = any(current.get(k, 0) < previous.get(k, 0) for k in numeric)
    delta = {k: max(0, current.get(k, 0) - previous.get(k, 0)) for k in numeric}
    return {"latest": current, "previous": previous, "delta": delta, "restated": restated}
```

Classify documented active/review/terminal/error states. Store `meta`, `delivery`, `attribution`, `observations`, and `alert_state` namespaces under `performance`; retain older keys on partial updates. Use `SELECT ... FOR UPDATE` so concurrent hourly/daily writes do not lose data.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_performance.py tests/test_publications.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/performance.py src/peermarket_agent/publications.py src/peermarket_agent/db/migrations.py tests/test_performance.py tests/test_publications.py
git commit -m "feat: persist Meta delivery performance"
```

### Task 4: Hourly collection, aggregate attribution reader, and alerts

**Files:**
- Modify: `src/peermarket_agent/config.py`
- Modify: `src/peermarket_agent/mcp_servers/peermarket_readonly.py`
- Modify: `src/peermarket_agent/agent/loops/hourly.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Create: `tests/test_attribution_reader.py`
- Modify: `tests/test_agent_hourly_loop.py`

**Interfaces:**
- Produces: `PeermarketReadonly.fetch_attribution(start, stop) -> list[AttributionAggregate]` using only `marketing_attribution_daily`.
- Produces: `collect_meta_performance(engine, settings, peermarket, notifier, now=None) -> CollectionResult`.
- Settings flags default false: `meta_insights_enabled`, `peermarket_attribution_enabled`.

- [ ] **Step 1: Write failing isolation and aggregate-reader tests**

```python
async def test_attribution_reader_queries_only_aggregate_view(readonly):
    await readonly.fetch_attribution(date(2026, 7, 15), date(2026, 7, 16))
    assert "marketing_attribution_daily" in readonly.executed_sql
    assert "campaign_touches" not in readonly.executed_sql

async def test_one_meta_failure_does_not_block_other_publications(engine, collector):
    collector.fail_for("ad-1")
    result = await collect_meta_performance(engine, SETTINGS, PEERMARKET, NOTIFIER)
    assert result.failed == [156]
    assert result.updated == [157]

async def test_no_delivery_alert_is_deduplicated(engine, notifier):
    await collect_meta_performance(engine, SETTINGS, PEERMARKET, notifier, now=NOW)
    await collect_meta_performance(engine, SETTINGS, PEERMARKET, notifier, now=NOW)
    notifier.notify_founder.assert_awaited_once()
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_attribution_reader.py tests/test_agent_hourly_loop.py`

Expected: reader/collector are absent.

- [ ] **Step 3: Implement feature-gated orchestration**

```python
async def run_hourly_pulse(engine, peermarket, *, settings=None, notifier=None):
    await _record_heartbeat_and_site_kpis(engine, peermarket)
    if settings and settings.meta_insights_enabled:
        await collect_meta_performance(engine, settings, peermarket, notifier)
```

Fetch statuses plus Insights per publication, save each independently, read attribution only when its flag is enabled, and alert on new `no_delivery`/`rejected_or_error` state or recovery. Missing aggregate view/permission stores a sanitized attribution diagnostic and sends one alert without failing Meta collection.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest -q tests/test_attribution_reader.py tests/test_agent_hourly_loop.py tests/test_performance.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/config.py src/peermarket_agent/mcp_servers/peermarket_readonly.py src/peermarket_agent/agent/loops/hourly.py src/peermarket_agent/agent/main.py tests/test_attribution_reader.py tests/test_agent_hourly_loop.py
git commit -m "feat: monitor Meta delivery hourly"
```

### Task 5: Daily summaries, observations, and evidence thresholds

**Files:**
- Create: `src/peermarket_agent/agent/loops/performance_daily.py`
- Create: `src/peermarket_agent/learnings.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Create: `tests/test_performance_daily.py`
- Create: `tests/test_learnings.py`

**Interfaces:**
- Produces: `evaluate_publication(performance: dict) -> EvidenceObservation`.
- Produces: `eligible_learning(comparisons, thresholds) -> LearningDecision`.
- Produces: `run_daily_performance(engine, notifier, settings, now=None) -> int`.

- [ ] **Step 1: Write failing denominator and threshold tests**

```python
def test_missing_registration_data_is_unavailable_not_zero():
    observation = evaluate_publication({"meta": {"landing_page_views": 5}, "attribution": {"available": False}})
    assert observation.metrics["landing_to_registration"] is None

def test_single_ad_never_creates_reusable_learning():
    decision = eligible_learning([QUALIFIED_VARIANT], DEFAULT_THRESHOLDS)
    assert decision.eligible is False
    assert decision.reason == "requires_comparable_variants"

def test_conversion_learning_requires_ten_registrations_each():
    decision = eligible_learning([variant(registrations=10), variant(registrations=9)], DEFAULT_THRESHOLDS)
    assert decision.eligible is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_performance_daily.py tests/test_learnings.py`

Expected: evaluator modules are absent.

- [ ] **Step 3: Implement pure calculations and daily persistence**

```python
def safe_ratio(numerator: int | None, denominator: int | None) -> Decimal | None:
    if numerator is None or denominator in (None, 0):
        return None
    return Decimal(numerator) / Decimal(denominator)

def eligible_learning(variants, thresholds):
    if len(variants) < 2:
        return LearningDecision(False, "requires_comparable_variants")
    if any(v.impressions < thresholds.impressions or v.landing_page_views < thresholds.landing_page_views for v in variants):
        return LearningDecision(False, "insufficient_delivery_evidence")
    if any(v.registrations < thresholds.registrations for v in variants):
        return LearningDecision(False, "insufficient_conversion_evidence")
    return LearningDecision(True, "thresholds_met")
```

Create one immutable observation per publication/UTC window. Group only matching channel, objective, language, audience, and window definition. Reinforce `learnings` only for eligible comparisons. Format a daily Slack summary with explicit `unavailable` values, attribution window, samples, Ads Manager link, and no claim of causality from a single ad.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_performance_daily.py tests/test_learnings.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/agent/loops/performance_daily.py src/peermarket_agent/learnings.py src/peermarket_agent/agent/main.py tests/test_performance_daily.py tests/test_learnings.py
git commit -m "feat: summarize attributed campaign evidence"
```

### Task 6: CI variables, disabled-first rollout, and production verifier

**Files:**
- Modify: `.github/workflows/deploy.yml`
- Modify: `README.md`
- Create: `src/peermarket_agent/cli_performance.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli_performance.py`

**Interfaces:**
- Repository variables: `META_INSIGHTS_ENABLED`, `PEERMARKET_ATTRIBUTION_ENABLED`, `META_INSIGHTS_LOOKBACK_DAYS`, `META_NO_DELIVERY_GRACE_HOURS`, `LEARNING_MIN_IMPRESSIONS`, `LEARNING_MIN_LANDING_PAGE_VIEWS`, `LEARNING_MIN_REGISTRATIONS`.
- Produces CLI `peermarket-performance verify --draft-id <id>`; strictly read-only.

- [ ] **Step 1: Write failing CLI safety test**

```python
def test_verify_reports_sanitized_sources_without_mutation(runner, monkeypatch):
    result = runner.invoke(cli, ["verify", "--draft-id", "156"])
    assert result.exit_code == 0
    assert '"meta_available": true' in result.output.lower()
    assert '"attribution_available": false' in result.output.lower()
    pause_meta_ad.assert_not_called()
    activate_meta_ad.assert_not_called()
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest -q tests/test_cli_performance.py`

Expected: CLI is absent.

- [ ] **Step 3: Implement verifier and CI variable wiring**

```yaml
META_INSIGHTS_ENABLED: ${{ vars.META_INSIGHTS_ENABLED || 'false' }}
PEERMARKET_ATTRIBUTION_ENABLED: ${{ vars.PEERMARKET_ATTRIBUTION_ENABLED || 'false' }}
META_INSIGHTS_LOOKBACK_DAYS: ${{ vars.META_INSIGHTS_LOOKBACK_DAYS || '3' }}
META_NO_DELIVERY_GRACE_HOURS: ${{ vars.META_NO_DELIVERY_GRACE_HOURS || '2' }}
LEARNING_MIN_IMPRESSIONS: ${{ vars.LEARNING_MIN_IMPRESSIONS || '1000' }}
LEARNING_MIN_LANDING_PAGE_VIEWS: ${{ vars.LEARNING_MIN_LANDING_PAGE_VIEWS || '30' }}
LEARNING_MIN_REGISTRATIONS: ${{ vars.LEARNING_MIN_REGISTRATIONS || '10' }}
```

Write these values to `secrets.env` without treating them as secrets. The verifier checks publication existence, live read-only Meta Insights, aggregate-view availability, latest snapshot freshness, and feature flags. It prints IDs/status/counts only.

- [ ] **Step 4: Run full verification**

Run: `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && git diff --check`

Expected: all tests and checks pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/deploy.yml README.md src/peermarket_agent/cli_performance.py pyproject.toml tests/test_cli_performance.py
git commit -m "ci: deploy Meta performance controls safely"
```

### Task 7: Review, PR, CI deployment, and staged enablement

**Files:** No new implementation files.

- [ ] **Step 1: Run fresh complete verification**

Run: `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && git diff --check origin/main...HEAD`

Expected: all checks pass.

- [ ] **Step 2: Request independent safety and correctness review**

Review for hidden Meta mutations, credential leakage, incorrect cumulative deltas, timezone mismatch, lost JSON updates, alert spam, false zero conversions, and underpowered learning creation. Resolve all critical and important findings with regression tests.

- [ ] **Step 3: Push and open a reviewed PR**

```bash
git push -u origin feat/meta-attribution-learning
gh pr create --repo kobozo/peermarket-marketing-agent --base main --head feat/meta-attribution-learning --title "Add Meta performance and attribution learning" --body-file /tmp/jarvis-performance-pr.md
```

- [ ] **Step 4: Merge only after CI, then watch the automatic deploy**

```bash
PR=$(gh pr view --repo kobozo/peermarket-marketing-agent --json number --jq .number)
gh pr checks "$PR" --repo kobozo/peermarket-marketing-agent --watch
gh pr merge "$PR" --repo kobozo/peermarket-marketing-agent --squash --delete-branch
RUN=$(gh run list --repo kobozo/peermarket-marketing-agent --workflow deploy.yml --event push --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" --repo kobozo/peermarket-marketing-agent --exit-status
```

- [ ] **Step 5: Enable in two separate CI-backed configuration changes**

First set `META_INSIGHTS_ENABLED=true`, deploy, run the read-only verifier for draft 156, and confirm its €2.17-era history is treated as an unattributed baseline without Meta mutation. Only after PeerMarket's aggregate-view production evidence is green, set `PEERMARKET_ATTRIBUTION_ENABLED=true` through repository variables and trigger the same deployment workflow. Verify one consented test campaign aggregate and remove it through the normal cleanup path.
