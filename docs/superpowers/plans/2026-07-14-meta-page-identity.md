# Meta Page Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Supply the required Facebook Page identity to Meta ad creatives and preserve actionable API error details.

**Architecture:** Extend the existing environment-driven `Settings` and immutable `MetaConfig`, propagate the Page ID through the approval pipeline, and include it in `object_story_spec`. Extend the existing SDK exception boundary without exposing credentials.

**Tech Stack:** Python 3.12, pydantic-settings, Meta Business SDK, pytest, GitHub Actions, systemd

## Global Constraints

- Store `META_PAGE_ID=61592144690879` as a GitHub Actions repository secret.
- Deploy only through the existing push-to-`main` GitHub Actions workflow.
- Never log Meta credentials or tokens.
- Do not retry draft 156 automatically.

---

### Task 1: Page identity configuration and creative payload

**Files:**
- Modify: `tests/test_meta_ads.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_meta_pipeline.py`
- Modify: `src/peermarket_agent/config.py`
- Modify: `src/peermarket_agent/meta_ads.py`
- Modify: `src/peermarket_agent/meta_pipeline.py`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: environment variable `META_PAGE_ID`
- Produces: `MetaConfig.page_id: str` and `object_story_spec.page_id`

- [ ] **Step 1: Write failing regression tests**

Add assertions that a complete Meta configuration contains `page_id`, missing
`page_id` raises `MetaAdsDisabled`, the pipeline passes the configured Page ID,
and the creative payload contains `object_story_spec["page_id"]`.

- [ ] **Step 2: Verify the tests fail for the missing behavior**

Run: `uv run pytest tests/test_meta_ads.py tests/test_config.py tests/test_meta_pipeline.py -v`

Expected: failures showing `MetaConfig` has no `page_id` and the creative payload omits it.

- [ ] **Step 3: Implement minimal Page ID propagation**

Add `meta_page_id: str = ""` to `Settings`, `page_id: str` to `MetaConfig`, include
it in `_ensure_enabled`, pass it from `process_approved_meta_draft`, and emit:

```python
AdCreative.Field.object_story_spec: {
    "page_id": config.page_id,
    "link_data": link_data,
}
```

Expose `${{ secrets.META_PAGE_ID }}` in the deploy job and write
`META_PAGE_ID=$META_PAGE_ID` to the systemd environment file.

- [ ] **Step 4: Verify focused tests pass**

Run: `uv run pytest tests/test_meta_ads.py tests/test_config.py tests/test_meta_pipeline.py -v`

Expected: all focused tests pass.

### Task 2: Actionable Meta API diagnostics

**Files:**
- Modify: `tests/test_meta_ads.py`
- Modify: `src/peermarket_agent/meta_ads.py`

**Interfaces:**
- Consumes: `FacebookRequestError`
- Produces: credential-safe `MetaAdsError` text containing available Meta diagnostic fields

- [ ] **Step 1: Write a failing diagnostic regression test**

Construct a `FacebookRequestError` with message, code, subcode, user title, and
user message and assert the resulting `MetaAdsError` includes each field.

- [ ] **Step 2: Verify the diagnostic test fails**

Run: `uv run pytest tests/test_meta_ads.py -k api_failure -v`

Expected: failure because only the generic API message is currently preserved.

- [ ] **Step 3: Implement credential-safe formatting**

Build the error string only from `api_error_message()`, `api_error_code()`,
`api_error_subcode()`, `api_error_user_title()`, and `api_error_user_msg()` when
those values are present, then raise `MetaAdsError` from the SDK exception.

- [ ] **Step 4: Run focused and complete verification**

Run: `uv run pytest tests/test_meta_ads.py -v`

Run: `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -v`

Expected: lint, formatting, and all tests pass.

### Task 3: Configure and deploy through CI

**Files:**
- Modify: GitHub repository secret `META_PAGE_ID`
- Publish: tested repository changes

**Interfaces:**
- Consumes: repository secret and committed workflow
- Produces: healthy systemd deployment on the self-hosted runner

- [ ] **Step 1: Set the repository secret**

Run interactively without printing the value: `gh secret set META_PAGE_ID --repo kobozo/peermarket-marketing-agent`

- [ ] **Step 2: Commit and push the tested change**

Create an intentional fix commit and push it through the repository workflow.

- [ ] **Step 3: Monitor CI and deployment**

Use `gh run list` and `gh run watch` to require successful CI and deploy runs,
including the existing `127.0.0.1:8090/agent/healthz` smoke test.

- [ ] **Step 4: Report deployment outcome**

Report commit, workflow URLs, verification results, and that draft 156 remains
approved but was not automatically retried.
