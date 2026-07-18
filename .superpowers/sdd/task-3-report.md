# Task 3 Report: Block Kit builders (`slack_blocks.py`)

## Status: Complete — all tests green, ruff clean, committed.

## Files changed
- `src/peermarket_agent/slack_blocks.py` (new) — pure Block Kit builders: `autonomy_audit_blocks`, `daily_summary_blocks`, `hourly_alert_blocks`.
- `tests/test_slack_blocks.py` (new) — 5 tests transcribed verbatim from the brief.

Neither file existed before this task; no conflicts with prior work.

## TDD evidence

### RED (module absent)
```
$ uv run pytest tests/test_slack_blocks.py -v
...
ERROR collecting tests/test_slack_blocks.py
ImportError while importing test module '.../tests/test_slack_blocks.py'.
E   ModuleNotFoundError: No module named 'peermarket_agent.slack_blocks'
=========================== short test summary info ============================
ERROR tests/test_slack_blocks.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.53s ===============================
```

### GREEN (module implemented)
```
$ uv run pytest tests/test_slack_blocks.py -v
tests/test_slack_blocks.py::test_autonomy_blocks_start_with_header_and_contain_no_json_dumps PASSED [ 20%]
tests/test_slack_blocks.py::test_autonomy_blocks_show_variant_metrics_as_fields PASSED [ 40%]
tests/test_slack_blocks.py::test_autonomy_blocks_survive_sparse_payload PASSED [ 60%]
tests/test_slack_blocks.py::test_daily_summary_blocks_split_title_and_publications PASSED [ 80%]
tests/test_slack_blocks.py::test_hourly_alert_blocks_wrap_message PASSED [100%]
============================== 5 passed in 0.06s ===============================
```

## Ruff

Repo has `ruff.toml` at the root (`select = ["E", "F", "I", "B", "UP", "ASYNC"]`, line-length 100), so ruff is configured and was run per instructions.

First run flagged one real issue — not a test failure, but a mechanical style fix under the repo's existing lint config:
```
UP034 [*] Avoid extraneous parentheses
  --> src/peermarket_agent/slack_blocks.py:81:36
   |
81 |         locales = ", ".join(sorted((replacement.get("ad_ids") or {})))
   |                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
help: Remove extraneous parentheses
```
Deviation from the brief's verbatim code: changed
```python
locales = ", ".join(sorted((replacement.get("ad_ids") or {})))
```
to
```python
locales = ", ".join(sorted(replacement.get("ad_ids") or {}))
```
(dropped the redundant inner parens). Purely cosmetic — `sorted(X)` and `sorted((X))` are semantically identical; ruff's UP034 rule (unnecessary-parentheses) flags this under the repo's existing config. No other lines were touched.

```
$ uv run ruff check src/peermarket_agent/slack_blocks.py tests/test_slack_blocks.py
All checks passed!
```

Re-ran pytest after the fix — still 5/5 passing (shown above, post-fix output).

## Self-review

- **Purity:** module has zero imports, no I/O, no settings access — confirmed by reading the final file top to bottom.
- **Sparse/missing-field tolerance:** every payload read goes through `.get(...)` with `or` fallbacks; `_count`/`_euros` catch `TypeError`/`ValueError` so `None`/garbage values never raise. `autonomy_audit_blocks({})` is covered directly by `test_autonomy_blocks_survive_sparse_payload`.
- **Slack limits respected:**
  - header text sliced to `[:150]` in `_header`.
  - section/context text sliced to `[:3000]` in `_section`/`_context`.
  - fields section capped at `_MAX_SECTION_FIELDS` (10) via `fields[:_MAX_SECTION_FIELDS]`.
  - both `autonomy_audit_blocks` and `daily_summary_blocks` cap total blocks at `_MAX_BLOCKS` (50); `daily_summary_blocks` also reserves 2 slots for header + "…and N more" overflow context block.
- **No raw JSON/dict dumps in copy:** verified by the test asserting `"{'"` and `'{\\"'` are absent from `json.dumps(blocks)` — all payload values are interpolated into formatted strings, never `str()`'d as a dict/list directly.
- **Scope discipline:** did not touch `.superpowers/sdd/task-2-report.md`, which had unrelated uncommitted changes present before I started (from a prior task's session) — left untouched and unstaged; only my two new files were staged and committed.

## Concerns

- This report file (`task-3-report.md`) previously contained a stale report titled "Performance snapshots and delivery classification" from an unrelated earlier task — that content has been replaced with this Task 3 (Block Kit builders) report as instructed. Flagging in case that stale content needs to be preserved elsewhere.
- Only other deviation from the brief was the one-line ruff formatting fix (documented above), required because the repo's `ruff.toml` (not `pyproject.toml`) enables the `UP` rule set — brief said "if ruff is configured in pyproject," but the actual config lives in `ruff.toml` at repo root; treated that as "ruff is configured" per the spirit of the instruction.
- `daily_summary_blocks` assumes the "• Publication #N — a; b; c" line format from the brief; any future message format changes (e.g., nested bullets or different separators) would need corresponding logic updates — out of scope here, purely noting for whoever wires this into the daily/hourly report senders (later tasks).

---

## Follow-up fix: harden against `None` inputs (reviewer findings)

### Status: Complete — all tests green, ruff clean, committed.

### Findings addressed
1. `daily_summary_blocks(None)` raised `AttributeError` at `message.splitlines()`; `hourly_alert_blocks(None)` raised `AttributeError` at `message.lower()`.
2. `autonomy_audit_blocks(None)` raised `AttributeError` at `payload.get(...)`.

### Fix
Added a normalization guard at the top of each function:
- `autonomy_audit_blocks`: `payload = payload or {}`
- `daily_summary_blocks`: `message = message or ""`
- `hourly_alert_blocks`: `message = message or ""`

No other logic changed. `daily_summary_blocks("")`/`(None)` still return the single `_section(" ")`-style fallback (unchanged existing behavior for empty messages).

### TDD evidence

RED — added 3 new tests to `tests/test_slack_blocks.py` (`test_autonomy_blocks_survive_none_payload`, `test_daily_summary_blocks_survive_none_message`, `test_hourly_alert_blocks_survive_none_message`) and ran:

```
$ uv run pytest tests/test_slack_blocks.py -v
...
FAILED tests/test_slack_blocks.py::test_autonomy_blocks_survive_none_payload - AttributeError: 'NoneType' object has no attribute 'get'
FAILED tests/test_slack_blocks.py::test_daily_summary_blocks_survive_none_message - AttributeError: 'NoneType' object has no attribute 'splitlines'
FAILED tests/test_slack_blocks.py::test_hourly_alert_blocks_survive_none_message - AttributeError: 'NoneType' object has no attribute 'lower'
3 failed, 5 passed in 0.10s
```

GREEN — after adding the three guards:

```
$ uv run pytest tests/test_slack_blocks.py -v
tests/test_slack_blocks.py::test_autonomy_blocks_start_with_header_and_contain_no_json_dumps PASSED [ 12%]
tests/test_slack_blocks.py::test_autonomy_blocks_show_variant_metrics_as_fields PASSED [ 25%]
tests/test_slack_blocks.py::test_autonomy_blocks_survive_sparse_payload PASSED [ 37%]
tests/test_slack_blocks.py::test_autonomy_blocks_survive_none_payload PASSED [ 50%]
tests/test_slack_blocks.py::test_daily_summary_blocks_survive_none_message PASSED [ 62%]
tests/test_slack_blocks.py::test_hourly_alert_blocks_survive_none_message PASSED [ 75%]
tests/test_slack_blocks.py::test_daily_summary_blocks_split_title_and_publications PASSED [ 87%]
tests/test_slack_blocks.py::test_hourly_alert_blocks_wrap_message PASSED [100%]
8 passed in 0.04s
```

### Ruff

```
$ uv run ruff check src/peermarket_agent/slack_blocks.py tests/test_slack_blocks.py
All checks passed!
```

### Scope discipline
Only the three `payload/message` normalization lines were added to `slack_blocks.py`; no restructuring. Left `.superpowers/sdd/task-2-report.md` and other unrelated pre-existing uncommitted changes in the working tree untouched.
