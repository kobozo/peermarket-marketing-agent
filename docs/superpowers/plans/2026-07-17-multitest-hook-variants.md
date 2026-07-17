# Multilingual Hook Variants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a CI-controlled, shadow-first experiment setup that creates exactly three NL/FR/EN hook variants for draft 156 and lets Jarvis compare them deterministically.

**Architecture:** Extend the existing autonomous replacement/publication bundle so an experiment is a stable group of three multilingual publications under one campaign. Persist experiment identity and frozen comparison controls with the existing decision/action records; expose a read-only inspection command and keep Meta mutations unreachable while shadow mode is enabled.

**Tech Stack:** Python 3.12, SQLAlchemy/asyncpg, PostgreSQL JSONB, Pydantic Settings, Meta Marketing API adapters, Click, pytest/pytest-asyncio, GitHub Actions.

## Global Constraints

- One campaign and one comparable ad set are used for the first experiment.
- The only primary experiment dimension is `hook`; audience, landing page, optimization, format, and budget envelope stay fixed.
- Exactly three deterministic variants are created, each with Dutch, French, and English copy.
- Evidence floors are 1,000 impressions, 30 landing-page views, and 10 attributed registrations per variant.
- Only completed Europe/Brussels windows are eligible.
- Shadow mode cannot reach a Meta mutation method.
- At most one replacement per rolling 24 hours, one total-budget increase per campaign, 24-hour cooldown, seven-day maximum test, 20% rolling budget growth, and EUR 20/day/campaign ceiling.
- All creation, activation, configuration, and deployment changes occur through GitHub Actions.
- Every decision stores stable experiment/publication identities, exact thresholds, evidence window, and an idempotency key.
- Credentials and raw creative payloads never appear in CLI or Slack audit output.

---

### Task 1: Experiment contracts and configuration

**Files:**
- Modify: `src/peermarket_agent/autonomy/contracts.py`
- Modify: `src/peermarket_agent/config.py`
- Test: `tests/test_autonomy_contracts.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Add immutable `HookExperiment`/`HookVariant` contracts with `experiment_id`, campaign/ad-set IDs, landing-page URL, fixed identity, and exactly three language bundles.
- Add `meta_autonomy_experiment_id` and `meta_autonomy_variant_count` settings with safe defaults and validation that the first experiment is allowlisted to campaign `120249125021520342` and count is exactly 3.

- [ ] Write failing tests for stable IDs, exactly three variants, NL/FR/EN completeness, fixed identity matching, invalid count, and non-allowlisted campaign rejection.
- [ ] Run `uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py`; confirm the new contract/settings assertions fail.
- [ ] Implement frozen dataclasses and typed settings validation without changing existing autonomy defaults.
- [ ] Re-run the focused tests; expect all new assertions to pass.
- [ ] Commit `feat: define multilingual hook experiment contracts`.

### Task 2: Durable experiment/publication identity

**Files:**
- Modify: `src/peermarket_agent/db/migrations.py`
- Modify: `src/peermarket_agent/autonomy/store.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_autonomy_store.py`

**Interfaces:**
- Persist `experiment_id`, `variant_id`, `language`, and frozen fixed-identity JSON on each experiment publication; expose `record_experiment`, `list_experiment_variants`, and idempotent `record_experiment_variant`.

- [ ] Add failing PostgreSQL tests for the unique `(experiment_id,variant_id,language)` key, append-only identity, duplicate replay, and partial bundle recovery.
- [ ] Run the focused migration/store tests and verify the expected schema/fixture failures.
- [ ] Add idempotent migration columns/indexes and transactional store methods; reject identity drift instead of overwriting it.
- [ ] Run focused migration/store tests against a clean PostgreSQL database.
- [ ] Commit `feat: persist hook experiment identities`.

### Task 3: Deterministic multilingual hook generation

**Files:**
- Create: `src/peermarket_agent/autonomy/hook_experiments.py`
- Modify: `src/peermarket_agent/autonomy/replacements.py`
- Test: `tests/test_hook_experiments.py`

**Interfaces:**
- Produce `build_hook_experiment(draft, brand_voice, seed) -> HookExperiment` with exactly three stable variants and idiomatic NL/FR/EN bundles.
- Produce `validate_hook_experiment(experiment, fixed_identity) -> None` and a deterministic `experiment_id`/variant ordering.

- [ ] Write failing tests for deterministic replay, three distinct hooks, idiomatic language metadata, same landing page/audience/optimization, and rejection of literal/missing translations.
- [ ] Run the focused tests and confirm missing module/contract failures.
- [ ] Implement deterministic seeded generation using the existing replacement prompt/style boundaries; do not call Meta or persist data from this pure module.
- [ ] Run focused tests and inspect generated payloads for secret/raw-token leakage.
- [ ] Commit `feat: generate deterministic multilingual hook variants`.

### Task 4: CI shadow setup and read-only inspection

**Files:**
- Modify: `src/peermarket_agent/agent/loops/autonomy.py`
- Modify: `src/peermarket_agent/cli_performance.py`
- Modify: `.github/workflows/deploy.yml`
- Create: `docs/multitest-hook-experiment-runbook.md`
- Test: `tests/test_autonomy_loop.py`
- Test: `tests/test_cli_performance.py`
- Test: `tests/test_deploy_workflow.py`

**Interfaces:**
- Add `prepare_hook_experiment(...) -> HookExperiment` to validate draft 156/campaign identity and queue no Meta mutation in shadow mode.
- Extend `peermarket-performance autonomy --draft-id 156` with sanitized experiment ID, variant IDs/languages, fixed-identity check, and readiness/blocked reason.
- Wire safe GitHub variables for experiment ID, variant count, and shadow-only setup; document CI dispatch/run verification and kill switch.

- [ ] Write failing loop/CLI/workflow tests asserting exactly three variants, no action queue in shadow, read-only output, exact campaign allowlist, and CI-only dispatch instructions.
- [ ] Run focused tests to observe failures.
- [ ] Implement orchestration and CLI projection using existing leases/audit paths; never import Meta mutation methods in the read-only command.
- [ ] Run focused tests, then `uv run pytest -q` with a clean PostgreSQL database.
- [ ] Commit `feat: wire shadow hook experiment setup`.

### Task 5: Real Meta bundle creation matrix and safety tests

**Files:**
- Modify: `src/peermarket_agent/autonomy/executor.py`
- Modify: `src/peermarket_agent/meta_ads.py`
- Modify: `src/peermarket_agent/autonomy/replacements.py`
- Test: `tests/test_autonomy_executor.py`
- Test: `tests/test_meta_ads.py`
- Test: `tests/test_autonomy_loop.py`

**Interfaces:**
- Use existing narrow adapters to create/activate the three hook bundles, verify every campaign/ad-set/ad/creative and language identity, and compensate/reconcile on partial failure.

- [ ] Add failing production-path tests for three-bundle creation, duplicate retry, resource drift, rate-limit failure, and shadow mutation isolation.
- [ ] Run tests and confirm failures before implementation.
- [ ] Implement the smallest extension of existing bundle creation leases and fenced cleanup; keep one fixed ad set and landing page.
- [ ] Run the real Task-4/Task-5 adapter matrix with only the external SDK boundary faked; assert exact three-language resources and no duplicate writes.
- [ ] Commit `feat: create guarded three-variant hook bundles`.

### Task 6: Evaluation, audit, and CI rollout verification

**Files:**
- Modify: `src/peermarket_agent/agent/loops/autonomy.py`
- Modify: `src/peermarket_agent/autonomy/policy.py`
- Modify: `docs/autonomous-ad-lifecycle-runbook.md`
- Test: `tests/test_autonomy_policy.py`
- Test: `tests/test_autonomy_loop.py`
- Test: `tests/test_deploy_workflow.py`

**Interfaces:**
- Evaluate all three variants as one comparable `hook` experiment; persist neutral decisions until all floors are met; emit campaign-scoped Slack audits containing experiment/variant IDs, thresholds, samples, evidence window, and next evaluation.

- [ ] Add failing tests for insufficient evidence, order-independent winner/loser selection, ties, stale attribution, policy-control drift, and complete sanitized audit content.
- [ ] Run focused tests and observe the expected failures.
- [ ] Implement deterministic evaluation and audit payloads using existing policy/store contracts and campaign cooldowns.
- [ ] Verify with `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, and `git diff --check`.
- [ ] Commit `feat: evaluate hook experiments with durable audits`.

### Task 7: CI-only canary

**Files:**
- Modify: `.github/workflows/deploy.yml`
- Modify: `docs/multitest-hook-experiment-runbook.md`

- [ ] Stage `META_AUTONOMY_ENABLED=true`, `META_AUTONOMY_SHADOW=true`, insights/attribution true, allowlist only `120249125021520342`, and the existing 20%/EUR20/24h/7-day controls through `gh variable set`.
- [ ] Dispatch the workflow with the run-ID correlation helper and require `gh run watch <id> --exit-status`.
- [ ] Run the deployed read-only CLI for draft 156 and verify exactly three variants, fixed landing-page/audience identity, sufficient/insufficient evidence state, no queued action, and durable Slack audit.
- [ ] Keep shadow true and execution writes disabled until an explicit later canary approval.
- [ ] Commit `docs: add hook experiment rollout runbook` and record the successful CI run in the handoff.
