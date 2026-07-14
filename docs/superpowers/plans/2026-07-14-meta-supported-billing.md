# Meta Supported Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use a Meta-supported ad-set billing event and prove draft 156 produces a complete paused ad.

**Architecture:** Change the existing Meta ad-set request from link-click billing to impression billing while preserving link-click optimization. Validate the request contract test-first, deliver through the existing PR/CI/deployment path, then execute and inspect the live draft-156 pipeline on `agent-jarvis`.

**Tech Stack:** Python 3.12, Meta Business SDK 25.0.1, pytest, Ruff, GitHub Actions, systemd

## Global Constraints

- Send `billing_event=IMPRESSIONS` and `optimization_goal=LINK_CLICKS`.
- Keep all created campaign, ad-set, creative, and ad resources paused.
- Deploy only through the existing GitHub Actions workflow.
- Never automatically activate spend.
- Completion requires concrete campaign, ad-set, creative, and ad IDs for draft 156.

---

### Task 1: Supported ad-set billing request

**Files:**
- Modify: `tests/test_meta_ads.py`
- Modify: `src/peermarket_agent/meta_ads.py`

**Interfaces:**
- Consumes: `_sync_create` ad-set parameters
- Produces: Meta ad-set request with impression billing and link-click optimization

- [ ] **Step 1: Write the failing regression test**

Add this test using the existing `_patch_meta_sdk` helper:

```python
async def test_create_paused_ad_uses_supported_billing_event(monkeypatch):
    fake = _patch_meta_sdk(monkeypatch)
    await create_paused_ad(
        config=_FULL_CONFIG,
        name="x",
        primary_text="x" * 150,
        headline="x",
        description="x",
        cta_type="LEARN_MORE",
        landing_page_url="https://x",
        image_bytes=None,
        audience_profile_key="declutterers",
        daily_budget_eur=5,
    )
    params = fake.create_ad_set.call_args.kwargs["params"]
    assert params["billing_event"] == "IMPRESSIONS"
    assert params["optimization_goal"] == "LINK_CLICKS"
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_meta_ads.py::test_create_paused_ad_uses_supported_billing_event -v`

Expected: FAIL because the actual billing event is `LINK_CLICKS`.

- [ ] **Step 3: Implement the minimal production change**

Change only the ad-set parameter in `src/peermarket_agent/meta_ads.py`:

```python
AdSet.Field.billing_event: "IMPRESSIONS",
AdSet.Field.optimization_goal: "LINK_CLICKS",
```

- [ ] **Step 4: Verify GREEN and regression safety**

Run: `uv run pytest tests/test_meta_ads.py -v`

Run: `uv run ruff check src tests && uv run ruff format --check src tests`

Expected: all Meta tests pass and Ruff exits zero.

- [ ] **Step 5: Commit the implementation**

```bash
git add tests/test_meta_ads.py src/peermarket_agent/meta_ads.py
git commit -m "fix: use supported Meta billing event"
```

### Task 2: CI delivery

**Files:**
- Publish: branch `fix/meta-billing-event`
- Merge through: GitHub pull request into `main`

**Interfaces:**
- Consumes: committed implementation
- Produces: deployed merge commit on `agent-jarvis`

- [ ] **Step 1: Push and open a PR**

Push `fix/meta-billing-event` and open a PR whose body records Meta error code
`100`, subcode `2446404`, and the local red/green evidence.

- [ ] **Step 2: Require complete CI**

Monitor the PR check until the Postgres-backed pytest job, Ruff check, and Ruff
format check pass.

- [ ] **Step 3: Merge and require deployment success**

Merge the PR, then monitor the deploy workflow until both systemd services are
active and `http://127.0.0.1:8090/agent/healthz` reports success.

### Task 3: Live draft-156 proof

**Files:**
- Execute: deployed `/opt/peermarket-agent` on `agent-jarvis`
- Inspect: transient retry output and service journal

**Interfaces:**
- Consumes: approved DB draft 156 and deployed Meta configuration
- Produces: complete paused Meta ad and Ads Manager URL

- [ ] **Step 1: Verify deployed request contract**

Read `/opt/peermarket-agent/src/peermarket_agent/meta_ads.py` and require the
deployed ad-set request to contain `billing_event: "IMPRESSIONS"`.

- [ ] **Step 2: Run exactly one retry**

Invoke `process_approved_meta_draft` for draft 156 through a transient systemd
unit using `/etc/peermarket-agent/secrets.env`.

- [ ] **Step 3: Audit runtime evidence**

Require log events `meta_ads.campaign_created`, `meta_ads.adset_created`,
`meta_ads.image_uploaded`, `meta_ads.creative_created`, `meta_ads.ad_created`,
and `meta_pipeline.success`, and record each concrete resource ID plus the Ads
Manager URL from the pipeline result/Slack notification.

- [ ] **Step 4: Handle any newly proven Meta constraint**

If a later API stage fails, preserve its exact code/subcode/user message, add a
new failing regression test, deploy the smallest evidenced correction through
the same CI path, and retry until Step 3 succeeds.
