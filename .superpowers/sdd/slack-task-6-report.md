# Slack revisions Task 6 pre-review report

## Handoff scope

This pass completed the local pre-review and documentation work only. It did
not push, open or merge a PR, deploy, inspect production, or send a live Slack
message. Those steps remain with the parent whole-feature review.

## Changes

- Expanded `README.md` with founder-DM thread routing, founder-only decision
  authority, explicit acknowledgement precedence, latest-variant decisions,
  revision lineage, the 15-second debounce, root-scoped generation leases,
  transactional persistence/outbox semantics, retry behavior, recovery, and
  safe observability guidance.
- Added `tests/test_deploy_workflow.py` to protect the existing CI-gated deploy
  contract: deploy depends on tests, all four Slack runtime values are sourced
  from GitHub secrets and written to `secrets.env`, and both agent services are
  restarted.
- Left `.github/workflows/deploy.yml` unchanged. The feature uses existing
  hourly scheduling in the agent and requires neither a new interval variable
  nor a new secret.

## Whole-feature spec audit

- Thread event filtering, unknown-root behavior, founder-only defense in depth,
  and explicit `✅/❌` acknowledgement precedence are implemented and covered.
- Feedback storage is idempotent, uses a 15-second cutoff, preserves Slack
  timestamp order, and leaves feedback arriving during generation for a later
  batch.
- Revision generation is action-aware, treats feedback/source as delimited
  data, reuses schema and quality checks, and does not confer approval.
- Lineage persistence, predecessor superseding, applied feedback, and revised
  approval enqueue are atomic. Latest-leaf-only decisions prevent stale or
  superseded publication.
- Root generation is serialized with durable owner/expiry leases rather than
  a connection-held advisory lock. Lease renewal and stale-owner guards cover
  long model calls and process death while avoiding an open DB connection.
- Root/thread Slack delivery is idempotent and lease-claimed. A revised draft
  and its outbox record survive Slack failure without regenerating; root
  binding occurs only after Slack confirms a successful root post.
- Agent startup and hourly loops cover revision processing and outbox retry;
  the deploy workflow restarts both the agent and Socket Mode bridge.

No unresolved Critical or Important local finding was identified. Live Socket
Mode subscription correctness, production migration/service health, and the
controlled no-spend Slack lineage exercise cannot be established locally and
are intentionally deferred to the parent deployment phase.

## Verification

- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q`
  — 302 passed in 32.41s.
- `uv run ruff format --check src tests` — 87 files already formatted.
- `uv run ruff check src tests` — all checks passed.
- PyYAML `safe_load` over repository and `.github` YAML — 2 files parsed.
- `git diff --check` — passed.
