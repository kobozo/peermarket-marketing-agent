# Meta Terminal Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, race-safe operator command that replaces one fully terminal stored Meta hierarchy without changing its approved budget.

**Architecture:** The existing pipeline advisory lock remains the serialization boundary. Inside it, replacement validates exact stored IDs, reads all three Meta statuses, atomically appends an immutable history entry and clears current IDs, and then calls the normal create/activate/finalize lifecycle. A JSONB history column preserves every old and partial replacement attempt.

**Tech Stack:** Python 3.12, Click, SQLAlchemy async, PostgreSQL JSONB/advisory locks, pytest/pytest-asyncio.

## Global Constraints

- Replacement is explicit operator CLI only and generic for any draft, including draft 156.
- Exact currently stored campaign, ad-set, creative, and ad IDs are required.
- All campaign/ad-set/ad configured and effective statuses must be `ARCHIVED` or `DELETED`.
- Refusal performs no database write and no Meta creation call.
- Approved budget is frozen; the replacement command has no budget option.
- One locked transition archives history and clears current IDs; normal publishing creates exactly one hierarchy.
- Partial creation IDs remain current and are recorded in history; no automatic second replacement occurs.

---

### Task 1: Durable replacement history

**Files:**
- Modify: `src/peermarket_agent/db/migrations.py`
- Modify: `src/peermarket_agent/publications.py`
- Test: `tests/test_publications.py`

**Interfaces:**
- Produces: `MetaPublication.replacement_history: list[dict]` and `begin_meta_terminal_replacement(engine, draft_id, expected_ids, terminal_statuses)`.

- [ ] Write failing persistence tests proving migration/read mapping and atomic exact-ID history transition.
- [ ] Run `pytest tests/test_publications.py -q` and confirm failures are caused by missing history support.
- [ ] Add the JSONB migration, typed field, query mapping, and single guarded SQL update.
- [ ] Re-run `pytest tests/test_publications.py -q` and confirm green.

### Task 2: Locked replacement pipeline

**Files:**
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Test: `tests/test_meta_pipeline.py`

**Interfaces:**
- Consumes: `get_meta_ad_statuses(config, ids)` and `begin_meta_terminal_replacement(...)`.
- Produces: `replace_terminal_meta_draft(engine, draft_id, settings, notifier, expected_ids) -> TerminalReplacementResult`.

- [ ] Write failing tests for exact-ID refusal, terminal status matrix, missing/unknown/mixed/nonterminal refusal, frozen budget, partial failure retention, and concurrent duplicate prevention.
- [ ] Run the focused tests and confirm RED for missing replacement behavior.
- [ ] Implement validation/status preflight/history transition beneath the existing advisory lock and call the normal lifecycle once.
- [ ] Re-run focused pipeline tests and confirm GREEN.

### Task 3: Explicit truthful CLI

**Files:**
- Modify: `src/peermarket_agent/cli_meta.py`
- Test: `tests/test_cli_meta.py`

**Interfaces:**
- Produces: `peermarket-meta replace-terminal-draft --draft-id ... --campaign-id ... --adset-id ... --creative-id ... --ad-id ...`.

- [ ] Write failing tests proving all IDs are required, no budget flag exists, and result/refusal output identifies old/current state.
- [ ] Run focused CLI tests and confirm RED.
- [ ] Add the explicit command and truthful rendering.
- [ ] Re-run focused CLI tests and confirm GREEN.

### Task 4: Verification and handoff

**Files:**
- Create: `.superpowers/sdd/terminal-replacement-report.md`

- [ ] Run the full test suite with the configured test DSN.
- [ ] Run Ruff format/check, YAML parsing, and inspect `git diff --check` plus the complete diff.
- [ ] Re-read the amendment line by line and self-review races, failure boundaries, messages, and secret handling.
- [ ] Write the report with RED/GREEN evidence, verification commands, and concerns.
- [ ] Commit the complete amendment on `feat/meta-terminal-replacement`.
