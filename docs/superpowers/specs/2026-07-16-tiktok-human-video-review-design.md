# TikTok Human Video Review Design

## Goal

Turn an approved TikTok script into a Slack-thread recording workflow: the
founder records one or more clips, uploads them as replies, and the agent
validates, optionally combines, and reports on the resulting video without
publishing to TikTok.

## Scope

This phase covers:

- Linking a persisted TikTok draft to its Slack root message.
- Receiving video uploads in that draft's Slack thread.
- Downloading and locally storing supported video files with bounded size.
- Technical validation with `ffprobe`.
- Visual review using sampled frames and Claude vision.
- Combining multiple clips into a vertical MP4 with `ffmpeg`.
- Posting validation, errors, and output links back to the same thread.

This phase does not cover TikTok API publishing, automatic posting, Grok API
generation, face/voice replacement, or autonomous rewriting of the founder's
message. AI improvement means a review report and deterministic media cleanup,
not changing the speaker or claim without approval.

## User flow

1. The agent sends a TikTok script as a Slack root message and records its
   `channel_id` and `message_ts` against the draft.
2. The founder replies in that thread with one or more video files.
3. Each supported upload is acknowledged immediately and processed in the
   background.
4. The agent checks the media, samples representative frames, and reviews the
   approved script against the footage.
5. For one clip, the agent posts a validation report and an optional cleaned
   export.
6. For multiple clips, the agent sorts them by upload order, creates a
   deterministic concatenated export, and posts the result plus review notes.
7. The founder makes the final TikTok upload manually.

## Architecture

### Slack thread intake

The outbound draft notifier must return the Slack message timestamp and store
the channel/timestamp pair in draft metadata. The Slack bridge must recognize
message events containing files and a `thread_ts`, ignore bot messages, and
accept only threads whose root metadata maps to a queued or approved TikTok
draft. It must not process arbitrary Slack uploads.

Slack file metadata is resolved through the Slack Web API. The bot token is
used to download the private file URL; the file is never exposed publicly.

### Media service

A focused media module owns:

- supported MIME/extension checks;
- maximum file size and count limits;
- deterministic storage paths beneath a configured media root;
- `ffprobe` metadata extraction;
- keyframe extraction with `ffmpeg`;
- safe concat/transcode output.

The service returns structured results rather than Slack-specific messages so
it can be tested independently.

### AI review service

The review service receives the approved script, technical metadata, and a
small set of JPEG keyframes. It calls Claude with image content and requests a
strict JSON result containing:

- `decision`: `pass`, `needs_changes`, or `reject`;
- strengths;
- concrete changes;
- script alignment notes;
- suggested edit points;
- a short founder-facing summary.

Technical failures are deterministic and must not be hidden behind AI review.
If Claude is unavailable, the thread receives the technical result and a
retry instruction rather than a false pass.

### Persistence

Draft metadata stores the Slack root reference and the media review state.
A separate `video_assets` table records each upload and derived output. This
keeps the existing `drafts.asset_path` field compatible with existing code and
allows multiple source clips per draft.

Each asset records draft ID, Slack file ID, thread reference, local path,
media role (`source` or `combined`), MIME type, byte size, duration, width,
height, status, and JSON review metadata.

## Validation rules

- Accept MP4/MOV/WebM only when MIME and extension agree.
- Reject files over the configured per-file limit.
- Reject clips without a video stream.
- Warn when the clip is not vertical 9:16; the cleanup export converts it to
  1080x1920 with a centered fit and black/brand-neutral padding as needed.
- Warn when duration is under 3 seconds or over 60 seconds.
- Preserve source audio when present and normalize the combined export.
- Never overwrite an uploaded source file.
- Prevent path traversal by deriving all filenames from Slack file IDs.

## Combination rules

For multiple clips, upload order is the default order. The first combined
version uses the concat demuxer only after each source has been normalized to
the same H.264/AAC, 1080x1920, 30fps format. If a source cannot be normalized,
the agent reports the failing file and does not produce a partial result.

## Slack responses and failures

The thread receives concise progress states:

- upload accepted;
- technical validation complete;
- AI review complete;
- combined export ready;
- actionable error with the filename and next step.

Duplicate Slack file events are idempotent by `(draft_id, slack_file_id)`.
Concurrent uploads are serialized per draft so combined exports cannot race.
Temporary files and extracted frames are removed after processing; derived
combined output remains until the draft is closed or retention cleanup removes
it.

## Security and privacy

- Require Slack user identity to match the founder or an explicitly allowed
  reviewer before downloading media.
- Keep Slack tokens and Anthropic credentials server-side.
- Never put private Slack download URLs in a public message.
- Apply a retention period to local media and document the cleanup setting.
- Do not use AI to impersonate the founder or alter their spoken claims.

## Testing

Unit tests cover Slack thread/file event extraction, idempotency keys, media
validation, safe storage paths, ffprobe parsing, review JSON parsing, and Slack
message formatting. Integration tests cover draft metadata persistence,
thread matching, source/combined asset persistence, and the one-clip and
multi-clip workflows with subprocesses and external APIs stubbed. A small
fixture video is used for the media integration tests; no real Slack or Claude
credentials are required.

## Acceptance criteria

The feature is complete when a founder can reply with one video to an approved
TikTok script and receive a useful validation report in the same thread, and
when two or more valid replies produce one downloadable vertical MP4 plus an
AI review report. Invalid, oversized, unrelated, duplicate, and failed uploads
must produce safe, actionable thread replies without changing the draft's
approval state or publishing anything.
