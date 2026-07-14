# Task 5 Report: CI Configuration and Activation Guard

## Implemented

- Added `Settings.meta_auto_activate` with the safe default `False` and standard
  Pydantic environment boolean parsing.
- Added an early pipeline refusal for approved Meta drafts while automatic
  activation is disabled. The guard runs before screenshots, resource creation,
  publication persistence, or activation, and alerts the founder.
- Updated direct pipeline tests to opt into activation intentionally.
- Added deployment workflow propagation from the non-secret GitHub repository
  variable, falling back to `false`, without printing the environment file.
- Added a disposable `pgvector/pgvector:pg15` test job. Deployment now depends on
  the full pytest suite succeeding in that job.

## TDD Evidence

- RED: focused tests reported five configuration failures because the field was
  absent; the database-backed pipeline test proceeded into Meta creation while
  disabled.
- GREEN: the same focused slice passed: `10 passed`.

## Verification

- Full suite with disposable local pgvector endpoint: `188 passed in 13.34s`.
- `uv run ruff check src tests`: passed.
- Ruff format check for all Task 5 touched Python files: passed.
- Workflow YAML syntax checks for `ci.yml` and `deploy.yml`: passed.
- `git diff --check`: passed.

## Existing Concern

The repository-wide `uv run ruff format --check src tests` still reports three
pre-existing files from earlier feature tasks (`publications.py`,
`test_migrations.py`, and `test_publications.py`). Task 5 did not modify those
unrelated files; all Python files changed by Task 5 satisfy the formatter.

No repository variable was changed, no branch was pushed, and no deployment or
production reconciliation was attempted.
