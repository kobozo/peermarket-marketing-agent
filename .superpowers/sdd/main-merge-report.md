# Main Merge Report

## Resolution

- Merged `origin/main` into `feat/meta-activation-learning-loop` while preserving the feature branch's automatic-activation gate, durable Meta resource IDs, reconciliation, transactional rollback, and credential redaction.
- Added upstream `META_PAGE_ID` configuration and deployment secret propagation alongside `META_AUTO_ACTIVATE`.
- Applied the configured Page identity to Meta creatives and required it in the connector enablement gate.
- Preserved `billing_event=IMPRESSIONS`, `optimization_goal=LINK_CLICKS`, and explicit `targeting_automation.advantage_audience=0` for both audience templates.
- Combined upstream actionable Meta API error details with feature rollback metadata and credential redaction.
- Updated both initial creation and reconciliation activation paths to pass `page_id`.
- Retained and adapted tests from both sides to the feature branch's `create_meta_ad_paused` API.

## Verification

- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q`: **194 passed**.
- `uv run ruff format ...`: all selected merge files unchanged after formatting.
- `uv run ruff check ...`: all checks passed.
- `uv run python -c 'import yaml; yaml.safe_load(...)'`: deployment workflow parsed successfully.
- `git diff --cached --check`: no whitespace errors.
- Conflict-marker scan: no markers remain in `.github`, `src`, or `tests`.

## Concerns

- Production deployment now requires the `META_PAGE_ID` GitHub secret; automatic activation remains off unless `META_AUTO_ACTIVATE` is explicitly enabled.
- Meta API diagnostics can contain user-facing values supplied by Meta. App secret and system-user token values continue to be redacted before persistence or notification context is built.
