# TikTok Human Video Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a founder upload one or more recording clips in the Slack thread of an approved TikTok script, receive technical and AI validation, and obtain a combined vertical MP4 when multiple clips are supplied.

**Architecture:** Persist the Slack root reference and source/derived media assets in Postgres. Route Slack file-bearing thread messages into a background media workflow that downloads private files, validates and normalizes them with ffprobe/ffmpeg, samples frames, and asks Claude for a structured review. Keep Slack formatting, media processing, and AI review in separate modules; never publish to TikTok automatically.

**Tech Stack:** Python 3.12, Slack Bolt Socket Mode, Slack Web API, SQLAlchemy async/Postgres, Anthropic Claude vision, ffprobe/ffmpeg subprocesses, pytest/pytest-asyncio.

## Global Constraints

- No TikTok API publishing or automatic posting in this phase.
- No face replacement, voice cloning, or autonomous alteration of the founder's claims.
- Source files are immutable; derived exports use separate paths and rows.
- Only process video uploads from authorized users in a draft's Slack thread.
- Use test-first development: every production behavior starts with a failing test.
- Preserve existing Slack acknowledgement behavior for `✅ <draft_id>` and `❌ <draft_id>`.

---

### Task 1: Persist Slack thread references and video asset records

**Files:**
- Modify: `src/peermarket_agent/db/migrations.py`
- Modify: `src/peermarket_agent/drafts.py`
- Test: `tests/test_drafts.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- `Draft.metadata` continues to carry the Slack root reference as `{"slack_channel_id": str, "slack_ts": str}`.
- Add `VideoAsset` with fields `draft_id: int`, `slack_file_id: str`, `thread_ts: str`, `path: str`, `role: Literal["source", "combined"]`, `mime_type: str`, `size_bytes: int`, `duration_seconds: float | None`, `width: int | None`, `height: int | None`, `status: str`, and `review: dict`.
- Add `persist_video_asset(engine, asset) -> int` with `ON CONFLICT (draft_id, slack_file_id) DO NOTHING`, followed by a lookup that returns the existing row ID for duplicate uploads, and add `get_video_asset_by_slack_file(engine, draft_id, slack_file_id) -> VideoAsset | None`.

- [ ] **Step 1: Write failing persistence tests**

Add tests that persist a draft with Slack metadata, persist a source `VideoAsset`, retrieve it by `(draft_id, slack_file_id)`, and verify the second insert for the same pair returns the original row ID without duplication.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_drafts.py tests/test_migrations.py -q`

Expected: FAIL because the `video_assets` table and helper functions do not exist.

- [ ] **Step 3: Add the idempotent migration and helpers**

Add `video_assets` with a unique constraint on `(draft_id, slack_file_id)`, foreign-key reference to `drafts`, JSONB review metadata, and indexes on `(draft_id, created_at)`. Add the dataclass and parameterized SQL helpers in `drafts.py`.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_drafts.py tests/test_migrations.py -q`

Expected: PASS with no warnings.

- [ ] **Step 5: Commit the task**

Run: `git add src/peermarket_agent/db/migrations.py src/peermarket_agent/drafts.py tests/test_drafts.py tests/test_migrations.py && git commit -m "feat: persist Slack video assets"`

### Task 2: Thread-aware Slack draft messages and file intake

**Files:**
- Modify: `src/peermarket_agent/slack_notifier.py`
- Modify: `src/peermarket_agent/slack_bridge/app.py`
- Create: `src/peermarket_agent/slack_bridge/video_events.py`
- Test: `tests/test_slack_notifier.py`
- Test: `tests/test_slack_bridge.py`
- Test: `tests/test_video_events.py`

**Interfaces:**
- `SlackNotifier.post_draft_thread(draft_id: int, text: str) -> tuple[str, str]` returns `(channel_id, message_ts)`.
- `extract_video_upload(event: dict) -> VideoUpload | None` returns `VideoUpload(file_id, thread_ts, channel_id, user_id, filename, mimetype, size_bytes)`.
- `find_thread_draft(engine, channel_id: str, thread_ts: str) -> int | None` resolves only persisted TikTok drafts.

- [ ] **Step 1: Write failing parser and notifier tests**

Cover a file-bearing thread message, a non-thread file message, a non-video file, a bot message, and a notifier call that passes the returned Slack timestamp into draft metadata persistence.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_video_events.py tests/test_slack_bridge.py tests/test_slack_notifier.py -q`

Expected: FAIL because no video event extractor or thread-aware notifier exists.

- [ ] **Step 3: Implement event extraction and thread routing**

Handle Slack `message` events with `files` and `thread_ts`, reject bot events, check MIME/extension at the intake boundary, and dispatch accepted uploads to a background coroutine. Keep existing text acknowledgements unchanged.

- [ ] **Step 4: Implement root-message posting and metadata update**

Make the notifier post a root message with `chat_postMessage`, return its channel and timestamp, and add a helper that merges those fields into the draft's JSONB metadata.

- [ ] **Step 5: Run the focused tests and verify they pass**

Run: `pytest tests/test_video_events.py tests/test_slack_bridge.py tests/test_slack_notifier.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the task**

Run: `git add src/peermarket_agent/slack_notifier.py src/peermarket_agent/slack_bridge/app.py src/peermarket_agent/slack_bridge/video_events.py tests/test_slack_notifier.py tests/test_slack_bridge.py tests/test_video_events.py && git commit -m "feat: route TikTok video uploads from Slack threads"`

### Task 3: Secure Slack download and deterministic media validation

**Files:**
- Create: `src/peermarket_agent/video_media.py`
- Modify: `src/peermarket_agent/config.py`
- Modify: `pyproject.toml`
- Create: `tests/test_video_media.py`

**Interfaces:**
- `MediaLimits(max_bytes: int, max_clips: int, max_duration_seconds: int)`.
- `MediaMetadata(path: Path, mime_type: str, size_bytes: int, duration_seconds: float, width: int, height: int, has_audio: bool)`.
- `async download_slack_file(client, file_info: VideoUpload, destination: Path, max_bytes: int) -> Path`.
- `async inspect_video(path: Path) -> MediaMetadata`.
- `validate_media(metadata: MediaMetadata, limits: MediaLimits) -> list[str]`.
- `async normalize_video(source: Path, destination: Path) -> MediaMetadata`.

- [ ] **Step 1: Write failing media tests**

Test MIME/extension matching, size rejection, safe file naming from Slack IDs, ffprobe JSON parsing, warnings for non-vertical and out-of-range duration, and normalization command construction. Mock network and subprocess boundaries only.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_video_media.py -q`

Expected: FAIL because the media module and configuration limits do not exist.

- [ ] **Step 3: Add configuration and media primitives**

Add `VIDEO_MEDIA_ROOT`, `VIDEO_MAX_FILE_BYTES=209715200`, `VIDEO_MAX_CLIPS=8`, and `VIDEO_MAX_DURATION_SECONDS=60` settings. Implement private Slack download using the bot token, bounded streaming, ffprobe inspection, validation warnings, and ffmpeg normalization to H.264/AAC 1080x1920 30fps.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_video_media.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task**

Run: `git add src/peermarket_agent/video_media.py src/peermarket_agent/config.py pyproject.toml tests/test_video_media.py && git commit -m "feat: validate and normalize uploaded videos"`

### Task 4: Claude frame review and strict result parsing

**Files:**
- Create: `src/peermarket_agent/video_review.py`
- Modify: `src/peermarket_agent/claude.py`
- Create: `tests/test_video_review.py`
- Modify: `tests/test_claude.py`

**Interfaces:**
- `VideoReview(decision: Literal["pass", "needs_changes", "reject"], strengths: list[str], changes: list[str], script_alignment: list[str], edit_points: list[str], summary: str)`.
- `async extract_keyframes(video: Path, output_dir: Path, count: int = 6) -> list[Path]`.
- `async review_video(claude: ClaudeClient, script: str, metadata: MediaMetadata, frames: list[Path]) -> VideoReview`.

- [ ] **Step 1: Write failing review tests**

Test base64 image blocks are sent with the approved script, valid JSON parses into `VideoReview`, malformed JSON raises a clear `ValueError`, and a Claude failure is propagated for Slack retry handling.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_video_review.py tests/test_claude.py -q`

Expected: FAIL because Claude has no vision content method and the review parser does not exist.

- [ ] **Step 3: Implement vision request and strict parser**

Add a `complete_with_images` method to `ClaudeClient` that accepts ordered JPEG paths, constructs Anthropic image content blocks, and returns the existing `ClaudeResponse`. Implement keyframe extraction and a system prompt that forbids invented observations and requires the exact review schema.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_video_review.py tests/test_claude.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task**

Run: `git add src/peermarket_agent/video_review.py src/peermarket_agent/claude.py tests/test_video_review.py tests/test_claude.py && git commit -m "feat: review TikTok videos with Claude vision"`

### Task 5: One-clip and multi-clip workflow orchestration

**Files:**
- Create: `src/peermarket_agent/video_workflow.py`
- Modify: `src/peermarket_agent/slack_bridge/app.py`
- Modify: `src/peermarket_agent/slack_notifier.py`
- Create: `tests/test_video_workflow.py`

**Interfaces:**
- `async process_video_upload(engine, slack_client, claude, upload: VideoUpload, settings) -> WorkflowResult`.
- `async combine_video_clips(paths: list[Path], output: Path) -> MediaMetadata`.
- `WorkflowResult(status: Literal["accepted", "rejected", "reviewed", "combined", "failed"], message: str, output_path: Path | None)`.

- [ ] **Step 1: Write failing workflow tests**

Test authorized thread upload acceptance, duplicate idempotency, invalid-file reply, one-clip review reply, two-clip upload-order combination, and failure without a partial output. Stub Slack download, ffmpeg, ffprobe, Claude, database, and notifier boundaries.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_video_workflow.py -q`

Expected: FAIL because orchestration and combination functions do not exist.

- [ ] **Step 3: Implement the workflow**

Resolve the draft from the Slack thread, authorize the uploader, enforce clip count, persist the source asset before processing, download and inspect the file, normalize it, extract frames, review it against the draft copy, and post a thread reply. For multiple normalized sources, concatenate in upload order, persist the combined asset, and review the combined result.

- [ ] **Step 4: Add deterministic cleanup and per-draft locking**

Use an async lock keyed by draft ID, remove temporary frames and failed intermediate files, retain source and successful combined exports, and ensure duplicate events return the prior result without a second download.

- [ ] **Step 5: Run the focused tests and verify they pass**

Run: `pytest tests/test_video_workflow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the task**

Run: `git add src/peermarket_agent/video_workflow.py src/peermarket_agent/slack_bridge/app.py src/peermarket_agent/slack_notifier.py tests/test_video_workflow.py && git commit -m "feat: process Slack TikTok recording uploads"`

### Task 6: Generate recording briefs and connect approved TikTok drafts

**Files:**
- Modify: `src/peermarket_agent/prompts/tiktok_post.py`
- Modify: `src/peermarket_agent/agent/cli_draft.py`
- Modify: `src/peermarket_agent/slack_dm.py`
- Modify: `src/peermarket_agent/agent/loops/daily.py`
- Create or modify: `tests/test_prompts_tiktok.py`
- Modify: `tests/test_cli_draft.py`
- Modify: `tests/test_slack_dm.py`

**Interfaces:**
- Extend `TikTokPost` with `script`, `shots`, `on_screen_text`, and `recording_notes` while preserving `hook`, `body`, `cta`, and cost fields.
- `format_draft_dm` includes the recording brief and tells the founder to upload replies in the same Slack thread.
- Approval of a TikTok draft posts or updates the thread root reference without changing the existing approval semantics.

- [ ] **Step 1: Write failing prompt and formatting tests**

Assert generated JSON includes a 20-40 second spoken script, a bounded shot list, on-screen text, and recording notes; assert the Slack draft message instructs the founder to reply with one or more videos in the thread.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_prompts_tiktok.py tests/test_cli_draft.py tests/test_slack_dm.py -q`

Expected: FAIL because the current generator only returns hook/body/CTA and the Slack message has no recording instructions.

- [ ] **Step 3: Extend the generator and persisted metadata**

Update the exact JSON schema and constraints, preserve Dutch/French language parity, include the recording brief in `copy`, and store structured fields in draft metadata for the reviewer.

- [ ] **Step 4: Connect the approved draft to a Slack root message**

After the draft is persisted and notified, post a thread root with the recording brief and persist its channel/timestamp. Do not start media processing until a video reply arrives.

- [ ] **Step 5: Run the focused tests and verify they pass**

Run: `pytest tests/test_prompts_tiktok.py tests/test_cli_draft.py tests/test_slack_dm.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the task**

Run: `git add src/peermarket_agent/prompts/tiktok_post.py src/peermarket_agent/agent/cli_draft.py src/peermarket_agent/slack_dm.py src/peermarket_agent/agent/loops/daily.py tests/test_prompts_tiktok.py tests/test_cli_draft.py tests/test_slack_dm.py && git commit -m "feat: turn TikTok approvals into recording briefs"`

### Task 7: Configuration, operational documentation, and full verification

**Files:**
- Modify: `README.md`
- Modify: `deploy/systemd/slack-bridge.service`
- Create: `.env.example` if absent, otherwise modify it
- Modify: `tests/test_config.py`
- Modify: `tests/test_healthcheck.py`

**Interfaces:**
- Deployment creates the configured media directory with owner-only permissions and has ffmpeg/ffprobe available.
- README documents Slack app scopes, environment variables, retention, and the founder's upload workflow.

- [ ] **Step 1: Write failing configuration and documentation checks**

Test default media settings, configured overrides, and the healthcheck remaining available while media work runs in the background.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `pytest tests/test_config.py tests/test_healthcheck.py -q`

Expected: FAIL because the new settings and background workflow wiring are not documented or verified.

- [ ] **Step 3: Add deployment configuration and operational docs**

Document `files:read`, thread history/read, and message-posting scopes; add media-root and retention settings; create the media directory in installation; and document manual cleanup and the no-auto-publish boundary.

- [ ] **Step 4: Run the full test suite and static checks**

Run: `pytest -q`

Expected: PASS for all tests.

Run: `ruff check . && ruff format --check .`

Expected: no lint or formatting errors.

- [ ] **Step 5: Run the diff checks**

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Commit the task**

Run: `git add README.md deploy/systemd/slack-bridge.service .env.example tests/test_config.py tests/test_healthcheck.py && git commit -m "docs: configure Slack video review operations"`

## Execution order

Run Tasks 1 through 5 first to establish the end-to-end upload/review path.
Run Task 6 after the media workflow is stable so the generated recording brief
matches the finalized reviewer interface. Finish with Task 7 and the complete
verification suite.
