# Meta Activation and Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Slack-approved Meta drafts activate safely and add attributable Meta-to-PeerMarket performance learning.

**Architecture:** The marketing agent creates Meta resources paused, persists their IDs, activates and verifies them, and rolls back on failure. A separate insights service imports delivery metrics. PeerMarket captures first-party campaign attribution and exposes aggregate-only conversion counts through the existing read-only database boundary; a daily agent job converts joined evidence into bounded learnings.

**Tech Stack:** Python 3, asyncio, Meta Business SDK, SQLAlchemy/PostgreSQL, pytest, FastAPI/Starlette, GitHub Actions, systemd.

## Global Constraints

- Slack approval is permission to activate only the approved daily budget.
- No automatic budget increases, campaign pausing, or audience expansion.
- Draft 156 must reconcile its existing Meta IDs and never create duplicates.
- PeerMarket remains read-only from the marketing agent.
- No Meta Pixel or Conversions API in this release.
- Secrets live in GitHub Secrets; non-sensitive controls live in GitHub Variables.
- Deploy only through each repository's GitHub Actions workflow.

---

### Task 1: Persist a Reconciliable Meta Publication

**Files:**
- Modify: `src/peermarket_agent/db/migrations.py`
- Create: `src/peermarket_agent/publications.py`
- Modify: `tests/test_migrations.py`
- Create: `tests/test_publications.py`

**Interfaces:**
- Produces: `MetaPublication`, `get_meta_publication(engine, draft_id)`, `upsert_meta_publication(engine, publication)`, and `mark_meta_publication_active(engine, draft_id, statuses)`.
- Stores: unique `draft_id`, lifecycle state, all four Meta IDs, Ads Manager URL, approved budget, status JSON, failure JSON, and timestamps.

- [ ] **Step 1: Write failing migration and repository tests**

Test that migrations add a unique draft constraint plus explicit publication lifecycle and Meta metadata columns, and that an upsert preserves previously stored IDs when a later call omits them.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_migrations.py tests/test_publications.py -q`

Expected: failure because the columns and repository module do not exist.

- [ ] **Step 3: Implement the idempotent schema and repository**

Add nullable `state`, `external_ids`, `external_statuses`, `failure`, `approved_budget_cents`, `ads_manager_url`, and `updated_at` fields using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; add a unique index on non-null `draft_id`. Implement typed repository functions with `INSERT ... ON CONFLICT (draft_id) DO UPDATE` and JSON merge semantics.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_migrations.py tests/test_publications.py -q`

Expected: PASS.

Commit: `git commit -am "feat: persist reconciliable Meta publications"` after explicitly adding the new files.

### Task 2: Activate, Verify, and Roll Back Meta Resources

**Files:**
- Modify: `src/peermarket_agent/meta_ads.py`
- Modify: `tests/test_meta_ads.py`

**Interfaces:**
- Produces: `create_meta_ad_paused(...) -> MetaAdResult`, `activate_meta_ad(config, ids) -> MetaActivationResult`, `get_meta_ad_statuses(config, ids)`, and `pause_meta_ad(config, ids)`.
- `MetaActivationResult` contains configured/effective status for campaign, ad set, and ad.

- [ ] **Step 1: Write failing ordered-activation tests**

Test that resources are still created paused, activation calls campaign then ad set then ad, and verification accepts configured `ACTIVE` with effective `ACTIVE`, `IN_PROCESS`, or `PENDING_REVIEW`.

- [ ] **Step 2: Write failing rollback tests**

Make ad-set activation fail and assert best-effort pause calls occur ad → ad set → campaign. Assert the raised `MetaAdsError` includes phase, IDs, observed statuses, and rollback errors without exposing credentials.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/test_meta_ads.py -q`

Expected: new tests fail because activation/reconciliation functions are missing.

- [ ] **Step 4: Implement minimal connector behavior**

Use SDK resource `api_update(params={"status": ...})` calls, explicit status reads, a review-state allowlist, and child-to-parent rollback. Keep synchronous SDK work inside `asyncio.to_thread`. Rename the old public function only after updating all imports.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/test_meta_ads.py -q`

Expected: PASS.

Commit: `git commit -am "feat: activate and verify approved Meta ads"`.

### Task 3: Make the Approval Pipeline Idempotent and Complete

**Files:**
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Modify: `src/peermarket_agent/slack_bridge/ack_handler.py`
- Modify: `tests/test_meta_pipeline.py`
- Modify: `tests/test_ack_handler.py`

**Interfaces:**
- Consumes: publication repository and Meta activation functions from Tasks 1-2.
- Produces: an idempotent `process_approved_meta_draft(...)` that marks the draft published only after verified activation/submission to review.

- [ ] **Step 1: Write a failing fresh-publication test**

Assert create-paused → persist IDs → activate → verify → atomically set publication active and draft published. Assert Slack reports `ACTIVE` or the accepted Meta review state and no longer says manual activation is required.

- [ ] **Step 2: Write failing reconciliation and failure tests**

Seed existing IDs for draft 156 and assert the pipeline skips screenshot/creative creation and activates those IDs. Assert activation failure retains `approved`, persists diagnostics, rolls back, and sends one precise message. Assert a published retry is a no-op.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/test_meta_pipeline.py tests/test_ack_handler.py -q`

- [ ] **Step 4: Implement the orchestration**

Move lifecycle decisions into the pipeline, keep Slack handler limited to approval plus pipeline dispatch, and remove the false `Trust score updated` statement. Store the approved budget before any activation call.

- [ ] **Step 5: Run the complete suite and commit**

Run: `uv run pytest -q`

Expected: PASS.

Commit: `git commit -am "fix: complete Meta publishing after Slack approval"`.

### Task 4: Add a Safe Draft-156 Reconciliation Command

**Files:**
- Create: `src/peermarket_agent/cli_meta.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli_meta.py`

**Interfaces:**
- Produces CLI: `peermarket-meta reconcile-draft --draft-id 156 --campaign-id ... --adset-id ... --creative-id ... --ad-id ...`.
- Reuses the production pipeline; it does not implement a separate status-changing path.

- [ ] **Step 1: Write failing CLI tests**

Assert required IDs, dry-run status display, publication upsert, production activation dispatch, and refusal when stored IDs conflict with supplied IDs.

- [ ] **Step 2: Verify RED, implement, then verify GREEN**

Run: `uv run pytest tests/test_cli_meta.py -q`

Expected before implementation: FAIL; after implementation: PASS.

- [ ] **Step 3: Run suite and commit**

Run: `uv run pytest -q`

Commit: `git commit -am "feat: reconcile existing Meta draft resources"` after adding the new files.

### Task 5: Wire CI Configuration and Deploy the Activation Fix

**Files:**
- Modify: `src/peermarket_agent/config.py`
- Modify: `.github/workflows/deploy.yml`
- Modify: `tests/test_config.py` or create it if absent

**Interfaces:**
- Adds GitHub Variable `META_AUTO_ACTIVATE` with a safe default of `false` in code and explicit production value `true` in the repository.

- [ ] **Step 1: Write a failing configuration test**

Assert boolean parsing, default false, and that the pipeline refuses automatic activation when disabled.

- [ ] **Step 2: Implement config and workflow propagation**

Map `${{ vars.META_AUTO_ACTIVATE }}` into `secrets.env` without printing secrets. Add `uv run pytest -q` before the sync/deploy step so failing tests block deployment.

- [ ] **Step 3: Verify locally and commit**

Run: `uv run pytest -q`

Commit: `git commit -am "ci: configure automatic Meta activation"`.

- [ ] **Step 4: Push through CI and verify production**

Set repository variable `META_AUTO_ACTIVATE=true`, push the branch/PR, merge through the normal repository flow, and watch the deploy job to completion. Do not manually edit `/opt/peermarket-agent`.

- [ ] **Step 5: Reconcile draft 156**

Run the deployed CLI on `192.168.1.76` using the known existing IDs:

```text
campaign 120249097403640342
ad set 120249097403910342
creative 4567984846756564
ad 120249097407330342
```

First run dry-run, then production reconciliation. Query Meta afterward and verify configured campaign/ad-set/ad status `ACTIVE`, with ad effective state either delivering or valid Meta review. Verify exactly one publication and draft status `published`.

### Task 6: Import Meta Insights Idempotently

**Files:**
- Create: `src/peermarket_agent/meta_insights.py`
- Create: `src/peermarket_agent/agent/loops/meta_insights.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Modify: `src/peermarket_agent/config.py`
- Modify: `.github/workflows/deploy.yml`
- Create: `tests/test_meta_insights.py`
- Create: `tests/test_loops_meta_insights.py`

**Interfaces:**
- Produces: `fetch_publication_insights(config, ad_id, since, until)` and `run_meta_insights_sync(engine, settings, notifier)`.
- Updates cumulative snapshots in `publications.performance`, keyed by reporting date and attribution window.

- [ ] **Step 1: Write failing SDK parsing tests**

Cover pagination, empty delivery, spend/impressions/reach/link-click mapping, derived CTR/CPC/CPM, malformed action arrays, rate limiting, and permanent permission errors.

- [ ] **Step 2: Write failing loop tests**

Assert each publication is isolated, reruns upsert rather than add cumulative values, transient retry is bounded, and permanent errors create a deduplicated Slack alert.

- [ ] **Step 3: Verify RED, implement, and verify GREEN**

Run: `uv run pytest tests/test_meta_insights.py tests/test_loops_meta_insights.py -q`

- [ ] **Step 4: Add CI variables and commit**

Add `META_INSIGHTS_ENABLED`, `META_INSIGHTS_INTERVAL_MINUTES`, and `META_INSIGHTS_LOOKBACK_DAYS` as GitHub Variables propagated by CI.

Run: `uv run pytest -q`

Commit: `git commit -am "feat: import Meta ad insights"` after adding new files.

### Task 7: Add First-Party Attribution to the PeerMarket Repository

**Files (PeerMarket/secondhand repository):**
- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Create: `app/attribution.py`
- Modify: `app/main.py`
- Modify: `app/routers/auth.py`
- Modify: `app/routers/sell.py`
- Modify: `app/routers/identity.py`
- Modify: `app/i18n.py`
- Create: `tests/test_attribution.py`
- Modify: the PeerMarket GitHub Actions deployment workflow that currently deploys `secondhand`

**Interfaces:**
- Produces aggregate tables/views grouped by `utm_content` and UTC time bucket; marketing-agent receives no PII.
- Cookie stores only an opaque signed visitor ID and is first-party, secure, HTTP-only, SameSite=Lax.

- [ ] **Step 1: Write failing capture and linkage tests**

Cover allowed UTM length/character normalization, first/last touch, ignored unrelated query parameters, anonymous visitor creation, registration linkage, event deduplication, and no email/IP/session token in attribution rows.

- [ ] **Step 2: Write failing funnel-event tests**

Cover registration complete, first listing created, first listing published, and identity verification complete exactly once per user/visitor event key.

- [ ] **Step 3: Verify RED and implement schema/service/hooks**

Run: `pytest tests/test_attribution.py -q` in the PeerMarket repository. Implement focused service calls from existing route success points; do not spread parameter parsing across routers.

- [ ] **Step 4: Correct privacy copy in EN/NL/FR**

Remove the false statement that PeerMarket does not advertise. Describe limited first-party campaign attribution, its purpose, fields, retention, and absence of third-party pixels. Preserve the statement that no Meta Pixel/Google Analytics is loaded.

- [ ] **Step 5: Add retention and aggregate query tests**

Assert expired anonymous attribution is deleted and aggregate output contains time bucket, campaign/content keys, and counts only.

- [ ] **Step 6: Run PeerMarket suite and commit**

Run the repository's complete documented test command and migration checks.

Commit: `git commit -am "feat: track first-party campaign attribution"` after adding new files.

- [ ] **Step 7: Deploy only through PeerMarket CI**

Set non-sensitive retention configuration as a GitHub Variable, merge through the normal flow, verify CI, privacy page, database migration, and a synthetic `utm_content=test-ci` visit without retaining test rows.

### Task 8: Expose Aggregate Attribution to the Agent

**Files:**
- Modify: `src/peermarket_agent/mcp_servers/peermarket_readonly.py`
- Modify: `tests/test_peermarket_readonly.py` or create it if absent

**Interfaces:**
- Produces: `fetch_campaign_funnel(since, until) -> list[CampaignFunnelBucket]` using one fixed aggregate-only SELECT.

- [ ] **Step 1: Write a failing privacy-boundary test**

Assert the SQL selects only UTC bucket, sanitized UTM keys, event name, and aggregate count; reject dynamic SQL and any user, visitor, email, IP, or cookie columns.

- [ ] **Step 2: Implement the fixed query and verify**

Run: `uv run pytest tests/test_peermarket_readonly.py -q`

- [ ] **Step 3: Run suite and commit**

Run: `uv run pytest -q`

Commit: `git commit -am "feat: read aggregate campaign funnel metrics"`.

### Task 9: Generate Evidence-Bounded Learnings and Follow-ups

**Files:**
- Create: `src/peermarket_agent/learning_loop.py`
- Create: `src/peermarket_agent/agent/loops/daily_followup.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Modify: `src/peermarket_agent/prompts/meta_ad_creative.py`
- Modify: `src/peermarket_agent/slack_dm.py`
- Create: `tests/test_learning_loop.py`
- Create: `tests/test_daily_followup.py`
- Modify: `tests/test_prompts_meta.py`

**Interfaces:**
- Produces deterministic `calculate_publication_evidence(meta, funnel)`, `upsert_learning(engine, evidence)`, and `run_daily_followup(...)`.
- Prompt builder consumes only relevant, threshold-qualified learnings.

- [ ] **Step 1: Write failing metric tests**

Use fixed fixtures to verify cost per click, landing-to-registration, cost per registration, registration-to-listing, and cost per published listing, including zero denominators and mismatched windows.

- [ ] **Step 2: Write failing learning-threshold tests**

Assert insufficient samples create no conclusion; repeated equivalent evidence increments `seen_n_times`; confidence is deterministic and capped; evidence links contain publication IDs and metric windows.

- [ ] **Step 3: Write failing prompt/follow-up tests**

Assert prompts receive only matching channel/audience/language/objective learnings and Slack distinguishes delivery, conversion, and insufficient-data states. No recommendation may change budget automatically.

- [ ] **Step 4: Implement and verify**

Run: `uv run pytest tests/test_learning_loop.py tests/test_daily_followup.py tests/test_prompts_meta.py -q`

- [ ] **Step 5: Run all tests and commit**

Run: `uv run pytest -q`

Commit: `git commit -am "feat: learn from attributable campaign outcomes"` after adding files.

### Task 10: Final CI and Production Verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example` if present
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Documents repository variables, secret ownership, operational commands, data retention, and failure recovery.

- [ ] **Step 1: Add a CI contract test or workflow assertion**

Verify every new setting is declared in `Settings`, mapped from the correct GitHub Variable/Secret, written once into the environment file, and never echoed.

- [ ] **Step 2: Document operations**

Document draft reconciliation, Meta permission errors, rollback interpretation, Insights freshness, attribution retention, and how to disable the learning jobs without disabling ad publication.

- [ ] **Step 3: Run final verification**

Run: `uv run pytest -q`

Run: `git diff --check`

Run the PeerMarket repository's full test suite independently.

Expected: all tests pass and both worktrees are clean except intended commits.

- [ ] **Step 4: Deploy through both CI pipelines and audit**

Deploy PeerMarket attribution first, then the agent read/learning job. Confirm health checks, migrations, hourly Insights ingestion, aggregate funnel import, one daily follow-up, and no secrets or PII in logs/tables.

- [ ] **Step 5: Verify the success criteria**

Confirm draft 156 is attached to exactly one active Meta hierarchy, its publication stores current Insights, a controlled UTM visit appears only in aggregate attribution, and future Meta draft prompts can retrieve qualifying learnings without changing budget autonomously.
