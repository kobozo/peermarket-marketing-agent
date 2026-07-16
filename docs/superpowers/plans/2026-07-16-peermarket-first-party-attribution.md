# PeerMarket First-Party Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add consent-gated, first-party campaign attribution and an aggregate-only reporting view to `kobozo/secondhand`.

**Architecture:** A focused `app/attribution.py` service owns signed visitor identity, allowlisted campaign parameters, touches, and idempotent funnel events. FastAPI middleware captures consented landing touches; existing registration, listing, and identity transitions call the service. PostgreSQL exposes only a daily aggregate view to `marketing_readonly`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL 16, Jinja2, pytest, GitHub Actions, Docker Compose.

## Global Constraints

- Attribution is disabled until explicit analytics consent.
- Never persist email, IP address, display name, raw session token, or Meta credentials in attribution tables.
- Jarvis receives aggregate counts only; no user-level attribution rows.
- No Meta Pixel, Conversions API, Google Analytics, or third-party tracking scripts.
- Default retention is exactly 90 days and is controlled by `ATTRIBUTION_RETENTION_DAYS`.
- Consent and privacy copy must be correct in English, Dutch, and French.
- Deploy only through a reviewed PR and the existing GitHub Actions workflow.

---

### Task 1: Attribution schema, models, and aggregate view

**Files:**
- Modify: `/home/yannick/secondhand/app/models.py`
- Modify: `/home/yannick/secondhand/app/migrations.py`
- Modify: `/home/yannick/secondhand/app/config.py`
- Modify: `/home/yannick/secondhand/tests/test_marketing_readonly_role.py`
- Create: `/home/yannick/secondhand/tests/test_attribution_schema.py`

**Interfaces:**
- Produces: `CampaignTouch`, `CampaignEvent`, and SQL view `marketing_attribution_daily`.
- Produces: `Settings.attribution_retention_days: int = 90`.
- The view exposes only `day`, four UTM fields, `event_type`, and `event_count`.

- [ ] **Step 1: Write failing schema and grant tests**

```python
def test_attribution_view_contains_only_aggregate_columns(engine):
    run_migrations(engine)
    with engine.connect() as conn:
        columns = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='marketing_attribution_daily'
            ORDER BY ordinal_position
        """)).scalars().all()
    assert columns == [
        "day", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "event_type", "event_count",
    ]

def test_marketing_role_gets_view_not_raw_tables(monkeypatch):
    monkeypatch.setenv("MARKETING_READONLY_PASSWORD", "test-password")
    module = importlib.reload(importlib.import_module("app.migrations"))
    sql = "\n".join(module._STEPS)
    assert "GRANT SELECT ON marketing_attribution_daily TO marketing_readonly" in sql
    assert "GRANT SELECT ON campaign_touches" not in sql
    assert "GRANT SELECT ON campaign_events" not in sql
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest -q tests/test_attribution_schema.py tests/test_marketing_readonly_role.py`

Expected: failure because the models, tables, view, and grant do not exist.

- [ ] **Step 3: Add models and idempotent migration SQL**

```python
class CampaignTouch(Base):
    __tablename__ = "campaign_touches"
    id: Mapped[int] = mapped_column(primary_key=True)
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    utm_source: Mapped[str] = mapped_column(String(40))
    utm_medium: Mapped[str] = mapped_column(String(40))
    utm_campaign: Mapped[str] = mapped_column(String(100))
    utm_content: Mapped[str] = mapped_column(String(100))
    fbclid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_touched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_touched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

class CampaignEvent(Base):
    __tablename__ = "campaign_events"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_campaign_event_dedupe"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    touch_id: Mapped[int] = mapped_column(ForeignKey("campaign_touches.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

Add `CREATE TABLE IF NOT EXISTS`, indexes, a grouped `CREATE OR REPLACE VIEW marketing_attribution_daily`, and the view-only grant to `app/migrations.py`. Validate `ATTRIBUTION_RETENTION_DAYS` is between 1 and 365 in `app/config.py`.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest -q tests/test_attribution_schema.py tests/test_marketing_readonly_role.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/migrations.py app/config.py tests/test_attribution_schema.py tests/test_marketing_readonly_role.py
git commit -m "feat: add aggregate campaign attribution schema"
```

### Task 2: Consent controls and truthful policy copy

**Files:**
- Modify: `/home/yannick/secondhand/app/routers/pages.py`
- Modify: `/home/yannick/secondhand/app/main.py`
- Modify: `/home/yannick/secondhand/app/templates/_cookie_banner.html`
- Modify: `/home/yannick/secondhand/app/i18n.py`
- Modify: `/home/yannick/secondhand/tests/test_gdpr.py`
- Create: `/home/yannick/secondhand/tests/test_attribution_consent.py`

**Interfaces:**
- Produces: cookie `pm_analytics_consent=granted|denied`.
- Produces: `analytics_consent(request: Request) -> bool`.
- Produces: POST routes `/cookies/analytics/allow`, `/cookies/analytics/deny`, and `/me/privacy/analytics/withdraw`.

- [ ] **Step 1: Write failing consent and copy tests**

```python
def test_essential_only_sets_denied_cookie(client):
    response = client.post("/cookies/analytics/deny", follow_redirects=False)
    assert response.cookies["pm_analytics_consent"] == "denied"

def test_allow_analytics_sets_granted_cookie(client):
    response = client.post("/cookies/analytics/allow", follow_redirects=False)
    assert response.cookies["pm_analytics_consent"] == "granted"

@pytest.mark.parametrize("lang", ["en", "nl", "fr"])
def test_privacy_copy_discloses_first_party_campaign_measurement(client, lang):
    response = client.get(f"/lang/{lang}?next=/privacy", follow_redirects=True)
    assert "facebook pixel" in response.text.lower()
    assert "utm" in response.text.lower()
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest -q tests/test_attribution_consent.py tests/test_gdpr.py`

Expected: routes and new copy are absent.

- [ ] **Step 3: Implement explicit consent choices**

```python
ANALYTICS_CONSENT_COOKIE = "pm_analytics_consent"

def analytics_consent(request: Request) -> bool:
    return request.cookies.get(ANALYTICS_CONSENT_COOKIE) == "granted"

def _set_analytics_consent(value: Literal["granted", "denied"]):
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        ANALYTICS_CONSENT_COOKIE, value, max_age=31536000,
        httponly=True, secure=True, samesite="lax",
    )
    return response
```

Render two clearly labelled buttons in `_cookie_banner.html`. Replace every EN/NL/FR claim that PeerMarket does not advertise or perform analytics with accurate first-party attribution language, captured fields, 90-day default retention, withdrawal, and the explicit absence of third-party pixels.

- [ ] **Step 4: Run consent/privacy tests**

Run: `uv run pytest -q tests/test_attribution_consent.py tests/test_gdpr.py tests/test_pwa_install.py`

Expected: all pass; existing PWA consent behavior remains intact.

- [ ] **Step 5: Commit**

```bash
git add app/routers/pages.py app/main.py app/templates/_cookie_banner.html app/i18n.py tests/test_attribution_consent.py tests/test_gdpr.py
git commit -m "feat: add explicit campaign analytics consent"
```

### Task 3: Signed visitor identity, touch capture, and registration linking

**Files:**
- Create: `/home/yannick/secondhand/app/attribution.py`
- Modify: `/home/yannick/secondhand/app/main.py`
- Modify: `/home/yannick/secondhand/app/routers/onboarding.py`
- Create: `/home/yannick/secondhand/tests/test_attribution_capture.py`

**Interfaces:**
- Produces: `capture_campaign_touch(db, request, response) -> CampaignTouch | None`.
- Produces: `link_touch_to_user(db, request, user_id) -> None`.
- Produces: `record_campaign_event(db, touch, event_type, dedupe_key, user_id=None) -> bool`.
- Uses allowlist `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `fbclid` and signed cookie `pm_attr_visitor`.

- [ ] **Step 1: Write failing capture tests**

```python
def test_landing_without_consent_persists_nothing(client, db):
    client.get("/?utm_source=facebook&utm_medium=paid_social&utm_campaign=peermarket&utm_content=draft-200")
    assert db.query(CampaignTouch).count() == 0

def test_consented_landing_records_touch_and_daily_event(client, db):
    client.cookies.set("pm_analytics_consent", "granted")
    url = "/?utm_source=facebook&utm_medium=paid_social&utm_campaign=peermarket&utm_content=draft-200"
    first = client.get(url)
    client.get(url)
    assert first.cookies.get("pm_attr_visitor")
    assert db.query(CampaignTouch).count() == 1
    assert db.query(CampaignEvent).filter_by(event_type="landing_view").count() == 1

def test_registration_links_touch_and_records_once(client, db):
    client.cookies.set("pm_analytics_consent", "granted")
    client.get("/?utm_source=facebook&utm_medium=paid_social&utm_campaign=peermarket&utm_content=draft-200")
    signed_visitor = client.cookies.get("pm_attr_visitor")
    user = User(email="campaign-user@example.com", display_name="Campaign User")
    db.add(user)
    db.flush()
    request = SimpleNamespace(cookies={"pm_attr_visitor": signed_visitor})
    link_touch_to_user(db, request, user.id)
    link_touch_to_user(db, request, user.id)
    event = db.query(CampaignEvent).filter_by(event_type="registration_completed").one()
    assert event.user_id == user.id
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest -q tests/test_attribution_capture.py`

Expected: attribution service is missing.

- [ ] **Step 3: Implement the attribution service and middleware boundary**

```python
ALLOWED_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid")
EVENT_TYPES = {
    "landing_view", "registration_completed", "first_listing_created",
    "first_listing_published", "identity_verification_completed",
}

def record_campaign_event(db, touch, event_type, dedupe_key, user_id=None) -> bool:
    if event_type not in EVENT_TYPES:
        raise ValueError("unsupported campaign event")
    db.add(CampaignEvent(
        touch_id=touch.id, user_id=user_id, event_type=event_type,
        dedupe_key=dedupe_key, created_at=datetime.now(timezone.utc),
    ))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True
```

Use a response middleware only to attach the cookie; keep database work in a request-scoped helper so commits remain explicit. Validate lengths and allowed characters, ignore incomplete UTM sets, sign the random ID with a dedicated salt derived from `SESSION_SECRET`, and deduplicate landing views by visitor/content/UTC day. On registration, attach the internal user ID and record exactly one registration event.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest -q tests/test_attribution_capture.py tests/test_onboarding.py tests/test_security.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/attribution.py app/main.py app/routers/onboarding.py tests/test_attribution_capture.py
git commit -m "feat: capture consented campaign touches"
```

### Task 4: Funnel transitions, GDPR controls, and retention

**Files:**
- Modify: `/home/yannick/secondhand/app/routers/sell.py`
- Modify: `/home/yannick/secondhand/app/identity/stripe_provider.py`
- Modify: `/home/yannick/secondhand/app/routers/privacy_controls.py`
- Modify: `/home/yannick/secondhand/app/lifecycle.py`
- Create: `/home/yannick/secondhand/tests/test_attribution_funnel.py`
- Modify: `/home/yannick/secondhand/tests/test_gdpr.py`
- Modify: `/home/yannick/secondhand/tests/test_lifecycle.py`

**Interfaces:**
- Uses: `record_user_first_event(db, user_id, event_type, subject_id) -> bool`.
- Produces: `purge_expired_attribution(db, now=None) -> int`.

- [ ] **Step 1: Write failing funnel and privacy tests**

```python
def test_first_listing_created_and_published_are_each_recorded_once(client, db):
    user, touch = consented_user_with_touch(db)
    first = create_and_publish_listing(client, user)
    create_and_publish_listing(client, user)
    types = [row.event_type for row in db.query(CampaignEvent).filter_by(user_id=user.id)]
    assert types.count("first_listing_created") == 1
    assert types.count("first_listing_published") == 1

def test_first_verification_records_once(db):
    user, touch = consented_user_with_touch(db)
    complete_verification(db, user)
    complete_verification(db, user)
    assert event_count(db, user.id, "identity_verification_completed") == 1

def test_gdpr_export_and_anonymization_include_attribution(db):
    user, touch = consented_user_with_touch(db)
    assert _build_export(db, user)["campaign_attribution"]
    anonymize_user(db, user)
    assert db.query(CampaignTouch).filter_by(user_id=user.id).count() == 0
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest -q tests/test_attribution_funnel.py tests/test_gdpr.py tests/test_lifecycle.py`

Expected: transition events and privacy handling are absent.

- [ ] **Step 3: Add idempotent transition hooks and cleanup**

```python
def record_user_first_event(db, user_id: int, event_type: str, subject_id: int | str) -> bool:
    touch = latest_unexpired_touch_for_user(db, user_id)
    if touch is None:
        return False
    return record_campaign_event(
        db, touch, event_type,
        dedupe_key=f"{event_type}:user:{user_id}", user_id=user_id,
    )

def purge_expired_attribution(db, now=None) -> int:
    cutoff = now or datetime.now(timezone.utc)
    rows = db.query(CampaignTouch).filter(CampaignTouch.expires_at < cutoff).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)
```

Call the helper immediately after the first listing draft is created, after transition to `ACTIVE`, and inside Stripe's `first_time_verified` branch. Extend `_build_export`; delete/anonymize linked attribution during the existing delayed user anonymization; call retention cleanup from `run_housekeeping`.

- [ ] **Step 4: Run focused and full PeerMarket tests**

Run: `uv run pytest -q tests/test_attribution_funnel.py tests/test_gdpr.py tests/test_lifecycle.py tests/test_sell.py tests/test_publish.py tests/test_identity.py`

Then: `uv run pytest -q`

Expected: complete suite passes.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sell.py app/identity/stripe_provider.py app/routers/privacy_controls.py app/lifecycle.py tests/test_attribution_funnel.py tests/test_gdpr.py tests/test_lifecycle.py
git commit -m "feat: attribute first marketplace conversions"
```

### Task 5: CI variables and production rollout gate

**Files:**
- Modify: `/home/yannick/secondhand/.github/workflows/ci.yml`
- Modify: `/home/yannick/secondhand/README.md`
- Create: `/home/yannick/secondhand/scripts/verify_attribution.py`
- Create: `/home/yannick/secondhand/tests/test_verify_attribution_script.py`

**Interfaces:**
- Consumes repository variable `ATTRIBUTION_RETENTION_DAYS`, default `90`.
- Production verifier accepts a dedicated `utm_content=test-<run-id>` and removes test data through application code.

- [ ] **Step 1: Write the verifier contract test**

```python
def test_verifier_never_prints_identity_or_connection_values(monkeypatch, capsys):
    monkeypatch.setattr(verify_attribution, "run_probe", lambda: {"event_count": 1})
    assert verify_attribution.main() == 0
    assert capsys.readouterr().out.strip() == '{"event_count": 1, "status": "ok"}'
```

- [ ] **Step 2: Run it and confirm failure**

Run: `uv run pytest -q tests/test_verify_attribution_script.py`

Expected: verifier module is absent.

- [ ] **Step 3: Wire the variable and sanitized post-deploy probe**

```yaml
env:
  ATTRIBUTION_RETENTION_DAYS: ${{ vars.ATTRIBUTION_RETENTION_DAYS || '90' }}
```

Write the value to `.env`; after `/healthz`, run the verifier inside the app container. It must test essential-only non-persistence, consented event creation, daily aggregate visibility, absence of user columns, and cleanup. It prints counts/status only.

- [ ] **Step 4: Verify workflow and full suite**

Run: `uv run pytest -q && git diff --check`

Expected: all tests pass and diff check is clean.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml README.md scripts/verify_attribution.py tests/test_verify_attribution_script.py
git commit -m "ci: verify first-party attribution deployment"
```

### Task 6: Review, PR, CI deployment, and production evidence

**Files:** No new implementation files.

- [ ] **Step 1: Run final local verification**

Run: `uv run pytest -q && git diff --check origin/main...HEAD`

Expected: all tests pass and no whitespace errors.

- [ ] **Step 2: Request independent code and privacy-boundary review**

Review the complete diff for consent bypass, PII exposure, duplicate events, migration idempotency, misleading translations, and rollback safety. Resolve all critical and important findings with tests.

- [ ] **Step 3: Push and create a reviewed PR**

```bash
git push -u origin feat/first-party-attribution
gh pr create --repo kobozo/secondhand --base main --head feat/first-party-attribution --title "Add consent-aware campaign attribution" --body-file /tmp/peermarket-attribution-pr.md
```

- [ ] **Step 4: Merge only after CI is green, then watch deployment**

```bash
PR=$(gh pr view --repo kobozo/secondhand --json number --jq .number)
gh pr checks "$PR" --repo kobozo/secondhand --watch
gh pr merge "$PR" --repo kobozo/secondhand --squash --delete-branch
RUN=$(gh run list --repo kobozo/secondhand --workflow ci.yml --event push --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" --repo kobozo/secondhand --exit-status
```

- [ ] **Step 5: Keep Jarvis attribution disabled until production evidence passes**

Verify the consent UI in EN/NL/FR, execute the sanitized test campaign probe, query `marketing_attribution_daily` as `marketing_readonly`, and confirm raw tables are denied. Record only aggregate evidence in the rollout handoff.
