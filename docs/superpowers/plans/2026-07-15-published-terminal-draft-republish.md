# Published Terminal Meta Draft Republish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely republish draft 156 through the existing explicit terminal-replacement command while it remains `published` and its exact current Meta hierarchy is fully archived.

**Architecture:** Preserve the public approval pipeline's published-draft no-op. The explicit terminal-replacement service validates exact IDs and terminal statuses, records a guarded replacement attempt, then supplies a private attempt-scoped authorization to the internal publication lifecycle so it may rebuild a `published` draft without a temporary database downgrade.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy/PostgreSQL, Click, Meta Business SDK, pytest, GitHub Actions, systemd.

## Global Constraints

- Only the explicit `peermarket-meta replace-terminal-draft` route may republish a `published` draft.
- Supplied campaign, ad-set, creative, and ad IDs must exactly match the stored current IDs.
- Campaign, ad set, and ad must all have terminal configured and effective Meta states.
- Preserve the frozen approved budget and original creative metadata.
- Never persist a temporary `approved` status and never hand-edit production PostgreSQL.
- Ordinary Slack approvals, scheduled loops, retries, and `process_approved_meta_draft` calls keep treating `published` as a no-op.
- A failed replacement retains new partial IDs and diagnostics and never starts an automatic second hierarchy.
- Deploy only through PR and GitHub Actions before invoking the production replacement command.

---

### Task 1: Attempt-scoped republish of a published terminal draft

**Files:**
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Modify: `src/peermarket_agent/cli_meta.py`
- Modify: `tests/test_meta_pipeline.py`
- Modify: `tests/test_cli_meta.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `replace_terminal_meta_draft(...)`, the exact stored IDs, `begin_meta_terminal_replacement(...)`, and the existing internal `_process_approved_meta_draft(...)` lifecycle.
- Produces: published-terminal republish behavior only through `replace_terminal_meta_draft`; no new public automatic-pipeline interface.

- [ ] **Step 1: Write failing published-terminal happy-path tests**

Add a database-backed test that seeds a Meta draft as `published`, stores a complete current hierarchy and frozen budget, mocks every live Meta status as `ARCHIVED`, and invokes `replace_terminal_meta_draft(...)`. The mocked internal creation lifecycle must observe an explicit private replacement authorization, write different new IDs, and complete active publication state. Assert:

```python
assert result.old_ids == old_ids
assert result.current_ids == new_ids
assert result.state == "active"
assert stored.external_ids == new_ids
assert stored.replacement_history[-1]["old_ids"] == old_ids
assert stored.replacement_history[-1]["replacement_ids"] == new_ids
assert stored.replacement_history[-1]["state"] == "active"
assert draft_status == "published"
```

Record every observed draft status during the operation and assert it is never `approved`.

- [ ] **Step 2: Write failing isolation and refusal tests**

Add tests proving:

- direct `process_approved_meta_draft(...)` remains a no-op for `published`;
- direct `_process_approved_meta_draft(...)` without the private attempt authorization remains a no-op for `published`;
- an `approved` terminal replacement continues to work;
- published replacement refuses mismatched IDs, incomplete IDs, nonterminal states, missing frozen budget, incomplete creative metadata, disabled auto-activation, and incomplete connector configuration before `begin_meta_terminal_replacement` or external creation;
- two concurrent commands for the same old IDs create at most one new hierarchy;
- a failed published replacement retains `published`, partial new IDs, failure diagnostics, and exactly one finalized history entry.

- [ ] **Step 3: Verify RED**

Run:

```bash
AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test \
  uv run pytest tests/test_meta_pipeline.py tests/test_cli_meta.py -q
```

Expected: the new happy path fails with `draft #... is not approved` or the internal published no-op prevents creation.

- [ ] **Step 4: Add private attempt-scoped authorization**

Introduce a module-private immutable authorization value carrying at least `draft_id` and `attempt_id`, created only after `begin_meta_terminal_replacement(...)` succeeds. Extend `_process_approved_meta_draft(...)` with an optional private parameter defaulting to `None`.

The internal lifecycle may bypass its `published` early return only when:

```python
authorization is not None
and authorization.draft_id == draft_id
and authorization.attempt_id == attempt_id
```

Do not add this parameter to `process_approved_meta_draft(...)` or any Slack/scheduler interface. Keep the ordinary public path's published no-op test unchanged.

Allow `replace_terminal_meta_draft(...)` preflight to accept draft status `approved` or `published`; refuse every other status. After the guarded history transition returns `attempt_id`, construct the private authorization and pass it into the internal lifecycle.

- [ ] **Step 5: Preserve published status during finalization**

Refactor the successful internal lifecycle finalization so normal approved publication still uses the existing guarded `approved -> published` transition, while an authorized published replacement atomically updates publication state/statuses and confirms the draft is still `published` without rewriting it to `approved`.

The database write must fail closed if the draft status changed away from the authorization's expected `published` state. Publication active state, cleared failure, statuses, and current IDs must commit consistently with the guarded draft check.

- [ ] **Step 6: Keep CLI explicit and document it**

Keep all four exact ID options mandatory. Update command help and README to state that `replace-terminal-draft` supports an `approved` or already `published` draft only when the exact stored hierarchy is fully terminal, preserves the frozen budget, and creates spend after successful activation.

Document the required sequence:

```bash
peermarket-meta replace-terminal-draft \
  --draft-id 156 \
  --campaign-id 120249110304880342 \
  --adset-id 120249110305000342 \
  --creative-id 28047843224854442 \
  --ad-id 120249110305530342
```

State that operators must first inspect Meta and PostgreSQL and that the command must not be blindly retried after a failure.

- [ ] **Step 7: Verify focused and complete GREEN**

Run:

```bash
AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test \
  uv run pytest tests/test_meta_pipeline.py tests/test_cli_meta.py -q
AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test \
  uv run pytest -q
uv run ruff check src tests
git diff --check origin/main...HEAD
```

Expected: all tests pass, Ruff reports no errors, and the diff check is clean.

- [ ] **Step 8: Commit implementation**

```bash
git add src/peermarket_agent/meta_pipeline.py src/peermarket_agent/cli_meta.py \
  tests/test_meta_pipeline.py tests/test_cli_meta.py README.md
git commit -m "fix: republish published terminal Meta drafts"
```

- [ ] **Step 9: Review, CI deployment, and production draft-156 test**

Generate a whole-branch review package and fix all Critical/Important findings. Push `fix/republish-terminal-draft-156`, open a PR, wait for the full PostgreSQL-backed GitHub test job and review gates, squash-merge, and monitor the deployment through migrations and service health.

After deployment, re-read the four stored IDs and live terminal statuses. Invoke the exact command once on Jarvis (`192.168.1.121`). Then query sanitized PostgreSQL and Meta state to verify new IDs, one new finalized replacement-history entry, active/review statuses, frozen EUR 8 budget, `published` draft state, and no duplicate hierarchy. Do not execute a second replacement command as a retry.
