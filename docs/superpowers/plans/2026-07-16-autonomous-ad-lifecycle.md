# Autonomous Meta Ad Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Jarvis autonomously pause weak Meta ads, publish controlled replacements, reallocate spend, and scale proven winners under auditable evidence and budget guardrails.

**Architecture:** A pure deterministic policy creates frozen decisions from completed performance snapshots. A PostgreSQL action queue serializes one mutation per campaign; a separate executor revalidates live Meta state and configuration before calling narrow Meta mutation adapters. Shadow mode uses the same policy and persistence but makes the mutation adapter unreachable.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy/asyncpg, PostgreSQL JSONB, Pydantic Settings, Meta Business SDK, Click, pytest/pytest-asyncio, GitHub Actions, systemd.

## Global Constraints

- Tests are written and observed failing before production code changes.
- Only completed `Europe/Brussels` account-timezone windows are eligible.
- Initial floors are 1,000 impressions, 30 landing-page views, and 10 attributed registrations per comparable variant.
- No-delivery diagnosis begins after two configured-active hours with zero impressions.
- Maximum test duration is seven completed days; insufficient evidence never becomes a directional learning.
- One primary experiment dimension changes: `hook`, `copy`, `visual`, or `audience`.
- Replacements contain idiomatic Dutch, French, and English, never literal translations.
- At most one replacement and one total-budget increase per campaign in a rolling 24 hours.
- Every mutation creates a 24-hour campaign cooldown.
- Autonomous cumulative budget growth is at most 20% of the rolling-window opening budget and never above EUR 20 daily per campaign.
- Missing history, stale or contradictory data, ties, and unavailable attribution block directional action.
- Shadow mode cannot reach a Meta mutation method.
- Deployment and configuration changes occur only through GitHub Actions.

---

## File Structure

- `src/peermarket_agent/autonomy/contracts.py`: immutable decision/action contracts and enum values.
- `src/peermarket_agent/autonomy/policy.py`: pure evidence and budget decision rules.
- `src/peermarket_agent/autonomy/store.py`: transactional decision/action queue, leases, audit, and cooldown queries.
- `src/peermarket_agent/autonomy/executor.py`: execution state machine, invariant revalidation, rollback, and reconciliation blocking.
- `src/peermarket_agent/autonomy/replacements.py`: controlled multilingual replacement generation and experiment metadata.
- `src/peermarket_agent/agent/loops/autonomy.py`: evaluation, queueing, execution, and durable Slack orchestration.
- `src/peermarket_agent/meta_ads.py`: narrow status, budget, pause, and activation mutation adapters.
- `src/peermarket_agent/db/migrations.py`: autonomous decisions/actions schema and constraints.
- `src/peermarket_agent/config.py`: typed flags, allowlist, limits, and required ceiling.
- `.github/workflows/deploy.yml`: repository-variable-to-systemd configuration wiring.
- `src/peermarket_agent/cli_performance.py`: read-only shadow/canary inspection command.
- Focused tests mirror each module; existing integration tests cover startup and CI wiring.

---

### Task 1: Contracts, Configuration, and Durable Schema

**Files:**
- Create: `src/peermarket_agent/autonomy/__init__.py`
- Create: `src/peermarket_agent/autonomy/contracts.py`
- Modify: `src/peermarket_agent/config.py`
- Modify: `src/peermarket_agent/db/migrations.py`
- Modify: `.github/workflows/deploy.yml`
- Test: `tests/test_autonomy_contracts.py`
- Test: `tests/test_config.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_deploy_workflow.py`

**Interfaces:**
- Produces: `DecisionKind`, `ActionStatus`, `FrozenDecision`, and validated `Settings` fields used by every later task.
- Produces tables `autonomous_decisions`, `autonomous_actions`, and `autonomous_budget_events`.

- [ ] **Step 1: Write failing contract/config/schema tests**

```python
def test_autonomy_defaults_are_safe(settings_payload):
    settings = Settings(**settings_payload)
    assert settings.meta_autonomy_enabled is False
    assert settings.meta_autonomy_shadow is True
    assert settings.meta_autonomy_campaign_ids == ()
    assert settings.meta_autonomy_max_daily_budget_eur == 20
    assert settings.meta_autonomy_max_increase_percent == 20

def test_frozen_decision_rejects_scale_without_budget_values():
    with pytest.raises(ValueError):
        FrozenDecision(kind=DecisionKind.SCALE, campaign_id="1", evidence={}, reason="winner")
```

Assert migrations contain a unique decision key, one nonterminal action per campaign partial index, action status check, lease columns, JSONB before/after/audit fields, and budget-event cents plus timestamp. Assert the workflow writes every new setting to `secrets.env` with execution defaulting false and shadow defaulting true.

- [ ] **Step 2: Run the focused tests and observe failure**

Run: `uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py`

Expected: FAIL because autonomy contracts, settings, schema, and workflow variables do not exist.

- [ ] **Step 3: Implement contracts and safe validated settings**

Define string enums for `observe`, `pause`, `replace`, `reallocate`, `scale` and action states. Use a frozen dataclass whose `__post_init__` requires positive old/new cents for budget actions, an exact campaign ID, timezone-aware window timestamps, evidence, reason, and idempotency key. Add settings:

```python
meta_autonomy_enabled: bool = False
meta_autonomy_shadow: bool = True
meta_autonomy_campaign_ids_csv: str = ""
meta_autonomy_max_replacements_24h: int = Field(default=1, ge=0, le=10)
meta_autonomy_cooldown_hours: int = Field(default=24, ge=1, le=168)
meta_autonomy_max_test_days: int = Field(default=7, ge=1, le=30)
meta_autonomy_max_increase_percent: int = Field(default=20, ge=0, le=20)
meta_autonomy_max_daily_budget_eur: int = Field(default=20, ge=5, le=20)
```

Expose a parsed, trimmed, non-empty tuple property for the allowlist. Invalid IDs or an enabled non-shadow configuration with an empty allowlist must fail settings validation.

- [ ] **Step 4: Add migrations and CI variable wiring**

Create append-only decision rows, leased action rows linked to decisions, and budget event rows linked to actions. Add database constraints and indexes described above. Wire exact GitHub variables with the same defaults into the deploy environment and generated environment file.

- [ ] **Step 5: Run focused and full tests**

Run: `uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py && uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/autonomy src/peermarket_agent/config.py src/peermarket_agent/db/migrations.py .github/workflows/deploy.yml tests
git commit -m "feat: add autonomous lifecycle contracts and controls"
```

### Task 2: Deterministic Evidence and Budget Policy

**Files:**
- Create: `src/peermarket_agent/autonomy/policy.py`
- Test: `tests/test_autonomy_policy.py`

**Interfaces:**
- Consumes: `FrozenDecision`, settings thresholds, completed performance snapshots, action history, and budget events.
- Produces: `evaluate_campaign(snapshot, history, limits, now) -> FrozenDecision` with no I/O.

- [ ] **Step 1: Write the policy matrix as failing parameterized tests**

```python
@pytest.mark.parametrize("mutation", ["stale", "partial", "tie", "missing_attribution"])
def test_bad_evidence_always_observes(mutation, qualified_snapshot, limits, now):
    snapshot = mutate(qualified_snapshot, mutation)
    assert evaluate_campaign(snapshot, (), limits, now).kind is DecisionKind.OBSERVE

def test_scale_is_capped_from_rolling_opening_budget(winner, limits, now):
    decision = evaluate_campaign(
        winner, budget_history(opening=1000, current=1100), limits, now
    )
    assert decision.kind is DecisionKind.SCALE
    assert decision.old_budget_cents == 1100
    assert decision.new_budget_cents == 1200
```

Cover exact evidence floors, seven-day terminal observation, no-delivery diagnosis, comparable dimensions, neutral ties, 24-hour cooldown, prior replacement/increase, decreases not creating scale headroom, EUR 20 ceiling, missing budget history, reallocation preserving total, and exact Decimal comparisons.

- [ ] **Step 2: Run policy tests and observe import failure**

Run: `uv run pytest -q tests/test_autonomy_policy.py`

Expected: FAIL because `autonomy.policy` does not exist.

- [ ] **Step 3: Implement pure normalization and eligibility functions**

Implement focused private functions for completed-window validation, snapshot freshness, comparability, evidence floors, cooldowns, rolling opening budget, accumulated increases, and absolute ceiling. Reject booleans, floats, nonfinite numbers, negative counters, naive datetimes, and unknown dimensions at the boundary.

- [ ] **Step 4: Implement deterministic action selection**

Priority is: unsafe/unavailable `observe`; technical delivery failure `observe` with diagnosis reason; proven loser `replace`; proven winner with loser `reallocate`; proven winner with headroom `scale`; otherwise `observe`. Sort variants and histories by stable IDs and time so input order cannot alter the decision or idempotency key.

- [ ] **Step 5: Run tests including property-style boundaries**

Run: `uv run pytest -q tests/test_autonomy_policy.py tests/test_performance.py tests/test_learnings.py`

Expected: PASS with exact equality at 1,000/30/10, 20%, EUR 20, and 24-hour boundaries.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/autonomy/policy.py tests/test_autonomy_policy.py
git commit -m "feat: derive autonomous ad decisions from evidence"
```

### Task 3: Transactional Decision and Action Store

**Files:**
- Create: `src/peermarket_agent/autonomy/store.py`
- Test: `tests/test_autonomy_store.py`

**Interfaces:**
- Produces: `record_decision`, `enqueue_action`, `claim_next_action`, `begin_execution`, `finish_action`, `release_action`, `block_campaign_for_reconciliation`, `campaign_history`, and `record_budget_event`.
- Claims return a typed `ClaimedAction` and require a worker token for every transition.

- [ ] **Step 1: Write failing PostgreSQL concurrency tests**

Test duplicate decision idempotency, concurrent enqueue on one campaign, `FOR UPDATE SKIP LOCKED` claims, expired lease recovery, wrong-token rejection, valid state transitions, append-only evidence, budget history, and reconciliation blocking.

```python
first, second = await asyncio.gather(
    enqueue_action(engine, decision), enqueue_action(engine, decision)
)
assert sorted([first.created, second.created]) == [False, True]
assert await count_nonterminal(engine, decision.campaign_id) == 1
```

- [ ] **Step 2: Run store tests and observe failure**

Run: `uv run pytest -q tests/test_autonomy_store.py`

Expected: FAIL because store functions do not exist.

- [ ] **Step 3: Implement atomic persistence and leasing**

Use database time for leases, transactions for every transition, row locks for campaign serialization, unique-key conflict handling for idempotency, and JSON serialization that preserves Decimal values as strings. Never overwrite frozen decision evidence.

- [ ] **Step 4: Implement action finalization and audit writes**

Require the current claim token and expected status. Store sanitized failure category/message, before/after Meta state, rollback result, and next evaluation time. Insert successful budget writes into `autonomous_budget_events` in the same transaction as successful action finalization.

- [ ] **Step 5: Run focused and migration integration tests**

Run: `uv run pytest -q tests/test_autonomy_store.py tests/test_migrations.py`

Expected: PASS, including two simultaneous database connections.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/autonomy/store.py tests/test_autonomy_store.py
git commit -m "feat: persist and lease autonomous Meta actions"
```

### Task 4: Narrow Meta Mutation Adapters

**Files:**
- Modify: `src/peermarket_agent/meta_ads.py`
- Test: `tests/test_meta_ads.py`

**Interfaces:**
- Produces: `set_meta_ad_status(config, ad_id, status)`, `set_meta_adset_daily_budget(config, ad_set_id, cents)`, and `get_meta_budget_state(config, ids)`.
- All adapters return sanitized observed state and never persist database state.

- [ ] **Step 1: Write failing adapter tests with SDK fakes**

Verify exact resource targeting, positive integer cents, allowed statuses only, re-read verification, bound SDK API instance, credential redaction, rate-limit propagation, and that budget verification mismatch raises `MetaAdsError`.

```python
with pytest.raises(ValueError):
    await set_meta_adset_daily_budget(config, "123", True)
await set_meta_ad_status(config, "456", "PAUSED")
ad.api_update.assert_called_once_with(params={"status": "PAUSED"})
```

- [ ] **Step 2: Run adapter tests and observe failure**

Run: `uv run pytest -q tests/test_meta_ads.py -k 'status or budget_state or daily_budget'`

Expected: FAIL because the narrow adapters do not exist.

- [ ] **Step 3: Implement adapters with post-write verification**

Validate inputs before SDK initialization, execute blocking calls in `asyncio.to_thread`, bind the API object directly to resources, read back exact configured/effective status or daily budget, and raise sanitized structured errors on mismatch.

- [ ] **Step 4: Run all Meta connector tests**

Run: `uv run pytest -q tests/test_meta_ads.py tests/test_meta_insights.py`

Expected: PASS without weakening existing create/activate/pause rollback behavior.

- [ ] **Step 5: Commit**

```bash
git add src/peermarket_agent/meta_ads.py tests/test_meta_ads.py
git commit -m "feat: add verified Meta lifecycle mutations"
```

### Task 5: Controlled Multilingual Replacement Builder

**Files:**
- Create: `src/peermarket_agent/autonomy/replacements.py`
- Modify: `src/peermarket_agent/prompts/meta_ad_creative.py`
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Test: `tests/test_autonomy_replacements.py`
- Test: `tests/test_prompts_meta.py`
- Test: `tests/test_meta_pipeline.py`

**Interfaces:**
- Produces: `build_replacement(engine, claude, source, decision) -> ReplacementDraft` and `publish_replacement_paused(...) -> ReplacementPublication`.
- `ReplacementDraft` contains three independently authored locales, exactly one changed dimension, source/experiment IDs, frozen budget, landing URL, and `utm_content=draft-<new id>`.

- [ ] **Step 1: Write failing generation and publication tests**

Assert exact locale set `NL/FR/EN`, locale-specific prompt instructions, one changed dimension, maximum five matching valid learnings, unchanged non-test dimensions, stable UTM, brand validation, paused creation, and refusal when any source evidence or metadata differs from the frozen decision.

- [ ] **Step 2: Run focused tests and observe failure**

Run: `uv run pytest -q tests/test_autonomy_replacements.py tests/test_prompts_meta.py tests/test_meta_pipeline.py -k autonomous`

Expected: FAIL because the replacement builder and autonomous pipeline entry point do not exist.

- [ ] **Step 3: Implement schema-first multilingual generation**

Prompt Claude to author each language natively, return strict JSON, and label the one experiment dimension. Parse through the existing defensive JSON/schema machinery. Reject identical locale bodies, missing locales, literal locale markers, changed frozen fields, and invalid budgets.

- [ ] **Step 4: Add a paused-only autonomous publication entry point**

Reuse screenshot, brand frame, URL, archive, and `create_meta_ad_paused` primitives, but require a valid persisted autonomous decision and worker claim. Return paused IDs without activating or pausing the source; the executor owns ordering.

- [ ] **Step 5: Run replacement, prompt, and complete pipeline tests**

Run: `uv run pytest -q tests/test_autonomy_replacements.py tests/test_prompts_meta.py tests/test_meta_pipeline.py`

Expected: PASS, including existing founder-approved publication and terminal replacement flows.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/autonomy/replacements.py src/peermarket_agent/prompts/meta_ad_creative.py src/peermarket_agent/meta_pipeline.py tests
git commit -m "feat: build controlled multilingual ad replacements"
```

### Task 6: Executor, Rollback, and Reconciliation

**Files:**
- Create: `src/peermarket_agent/autonomy/executor.py`
- Test: `tests/test_autonomy_executor.py`

**Interfaces:**
- Produces: `execute_claim(engine, settings, meta, replacement_builder, claim, now) -> ExecutionResult`.
- Consumes Task 3 store functions, Task 4 adapters, and Task 5 paused replacement publication.

- [ ] **Step 1: Write the failing execution state-machine suite**

Cover shadow refusal, disabled flag, allowlist, stale snapshot, changed Meta IDs/status/budget, cooldown race, pause, replacement ordering, reallocation, scale, exact 20%/EUR 20 revalidation, rate limits, crashes after each external write, activation/pause split failure, rollback failure, and reconciliation block.

```python
await execute_claim(..., decision=replace_decision)
assert meta.calls == [
    "read_source", "create_paused", "read_replacement", "activate_replacement",
    "read_replacement", "pause_source", "read_source"
]
```

- [ ] **Step 2: Run executor tests and observe failure**

Run: `uv run pytest -q tests/test_autonomy_executor.py`

Expected: FAIL because the executor does not exist.

- [ ] **Step 3: Implement preflight and simple actions**

Re-read enabled/shadow/allowlist/limits, database publication, outstanding action, budget history, and live Meta state. Cancel stale actions. Implement pause, reallocate, and scale with exact before/write/read-after audit data.

- [ ] **Step 4: Implement replacement saga and compensation**

Create paused, verify, activate, verify, pause source, verify. On pre-activation failure pause all new resources. If source pause fails after activation, pause the replacement and verify it; if a single safe state cannot be proven, block the campaign for reconciliation. Never retry a non-idempotent creation without first reconciling stored resource IDs.

- [ ] **Step 5: Run executor plus Meta pipeline tests**

Run: `uv run pytest -q tests/test_autonomy_executor.py tests/test_meta_ads.py tests/test_meta_pipeline.py tests/test_autonomy_store.py`

Expected: PASS for every injected failure point and duplicate retry.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/autonomy/executor.py tests/test_autonomy_executor.py
git commit -m "feat: execute autonomous Meta actions safely"
```

### Task 7: Loop Orchestration, Durable Slack Audit, and CLI Inspection

**Files:**
- Create: `src/peermarket_agent/agent/loops/autonomy.py`
- Modify: `src/peermarket_agent/agent/loops/hourly.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Modify: `src/peermarket_agent/cli_performance.py`
- Test: `tests/test_autonomy_loop.py`
- Test: `tests/test_agent_hourly_loop.py`
- Test: `tests/test_agent_main.py`
- Test: `tests/test_cli_performance.py`

**Interfaces:**
- Produces: `run_autonomy_cycle(engine, claude, notifier, settings, now=None)` and read-only `peermarket-performance autonomy --draft-id 156`.
- The hourly pulse collects first; autonomy evaluates and executes only after successful collection.

- [ ] **Step 1: Write failing orchestration and CLI tests**

Assert collector-before-evaluator ordering, per-campaign isolation, shadow decisions persisted without action enqueue, one claimed action per campaign, Slack retry durability, startup safety, CLI sanitized evidence/action output, and CLI source containing no Meta mutation imports.

- [ ] **Step 2: Run focused orchestration tests and observe failure**

Run: `uv run pytest -q tests/test_autonomy_loop.py tests/test_agent_hourly_loop.py tests/test_agent_main.py tests/test_cli_performance.py`

Expected: FAIL because the loop and command do not exist.

- [ ] **Step 3: Implement the cycle and durable audit messages**

Load eligible allowlisted publications and their completed snapshots, call the pure policy, persist every decision, enqueue only executable non-shadow decisions, claim and execute bounded work, and persist Slack payload/delivery state for shadow, success, failure, rollback, recovery, and next-evaluation notices. One campaign failure must not prevent another campaign's evaluation.

- [ ] **Step 4: Wire startup/hourly execution and read-only inspection**

Pass Claude/settings/notifier explicitly. Run autonomy after performance collection at startup and hourly; disabled mode is a constant-time return. The CLI uses a PostgreSQL read-only transaction and reports flags, decision, evidence window, action/audit state, budgets, and reconciliation block without tokens or raw PeerMarket data.

- [ ] **Step 5: Run focused and full tests**

Run: `uv run pytest -q tests/test_autonomy_loop.py tests/test_agent_hourly_loop.py tests/test_agent_main.py tests/test_cli_performance.py && uv run pytest -q`

Expected: full suite PASS.

- [ ] **Step 6: Commit**

```bash
git add src/peermarket_agent/agent src/peermarket_agent/cli_performance.py tests
git commit -m "feat: run and inspect autonomous ad lifecycle"
```

### Task 8: Shadow-Mode CI Deployment and Draft 156 Canary Preparation

**Files:**
- Create: `docs/autonomous-ad-lifecycle-runbook.md`
- Test: `tests/test_deploy_workflow.py`

**Interfaces:**
- Produces a CI-only rollout runbook with exact variables, read-only verification, enable, kill-switch, and reconciliation commands.

- [ ] **Step 1: Write the failing rollout assertions**

Extend deploy tests to require safe workflow defaults and no literal credentials. Add a runbook test that checks exact staged variables, draft 156 allowlist, EUR 20 ceiling, shadow inspection command, service health command, and rollback through GitHub variables.

- [ ] **Step 2: Run deployment tests and observe failure**

Run: `uv run pytest -q tests/test_deploy_workflow.py`

Expected: FAIL until the runbook and all exact rollout controls exist.

- [ ] **Step 3: Write the exact operational runbook**

Document setting `META_AUTONOMY_ENABLED=false`, `META_AUTONOMY_SHADOW=true`, draft 156's campaign allowlist, `20` percent, EUR `20`, one replacement, seven days, and 24-hour cooldown through `gh variable set`; dispatching `deploy.yml`; inspecting `peermarket-performance autonomy --draft-id 156`; enabling only after the shadow record is valid; and disabling execution immediately through the master flag if reconciliation fails.

- [ ] **Step 4: Run all verification before publication**

Run: `uv run pytest -q && git diff --check && git status --short`

Expected: all tests PASS, no whitespace errors, only intentional runbook/test changes remain.

- [ ] **Step 5: Commit**

```bash
git add docs tests/test_deploy_workflow.py
git commit -m "docs: add autonomous lifecycle rollout runbook"
```

### Task 9: Whole-Branch Review and CI-Only Shadow Rollout

**Files:**
- Review all files changed since `origin/main`.

**Interfaces:**
- Produces a reviewed pull request and a deployed shadow decision for draft 156; autonomous Meta writes remain disabled at this task's end.

- [ ] **Step 1: Run final verification from a clean process**

Run: `uv sync --all-extras && uv run pytest -q && git diff --check origin/main...HEAD`

Expected: all tests PASS and no diff errors.

- [ ] **Step 2: Request independent specification and code-quality reviews**

Review every requirement in the design against implementation and inspect security, transaction boundaries, Meta ordering, exact budget arithmetic, idempotency, rollback, and secret redaction. Fix findings with new failing regression tests and separate commits.

- [ ] **Step 3: Publish through pull request and require green CI**

Push the branch, open a PR, wait for required checks and reviews, merge only when green, and let the main-branch GitHub Actions deployment run. Do not SSH-copy code or edit production configuration directly.

- [ ] **Step 4: Configure and deploy shadow mode through GitHub variables**

Set execution false, shadow true, allowlist only campaign `120249125021520342`, maximum increase 20, ceiling 20, replacement count 1, cooldown 24, and test duration 7. Dispatch the workflow and require successful tests, deploy, service restart, and healthcheck.

- [ ] **Step 5: Verify draft 156 shadow evaluation read-only**

Run the deployed read-only CLI under the service environment. Confirm the frozen evidence window, exact decision/reason, unchanged Meta IDs/status/budget, no autonomous action execution, durable Slack audit, and healthy services. If any invariant fails, keep execution disabled and fix through a new PR.

- [ ] **Step 6: Present the canary decision for explicit rollout continuation**

Report the shadow finding and proposed first action. Autonomous execution may be enabled through CI only after this production evidence is available; the master kill switch and allowlist remain in place.
