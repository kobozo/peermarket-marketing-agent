# peermarket-marketing-agent

Self-extending marketing agent for PeerMarket. Runs on Proxmox VM 129 (`agent-peermarket`).

See spec: `kobozo/secondhand → docs/superpowers/specs/2026-05-23-marketing-agent-design.md`.

## Local dev

```bash
uv sync --all-extras
docker run --rm -d --name agent-test-db -p 55432:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=agent_test \
  pgvector/pgvector:pg15
export AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test
uv run pytest -v
```

## Slack video review operations

The Slack bridge accepts founder-uploaded recording clips only when they are
replies in an active TikTok draft thread. It downloads the private Slack file,
checks it with `ffprobe`, normalizes it with `ffmpeg`, and posts the review
result back into that same thread. This is a review and intake workflow only:
it never auto-publishes to TikTok or any other channel. A human must make the
final publishing decision through the existing approval workflow.

### Slack app setup

The bot token needs these scopes:

- `files:read` to retrieve uploaded video metadata and private download URLs.
- `chat:write` to create draft threads and post review replies.
- `channels:history`, `groups:history`, `im:history`, and `mpim:history` to
  read the parent message and replies when authorizing a draft thread.
- `app_mentions:read` and the message event subscriptions used by the bridge
  so it can receive mentions and messages.

The app-level token needs `connections:write` for Socket Mode. Subscribe to
`app_mention` and message events, including direct messages. The bot must be
installed in any channel where it creates or reads a draft thread.

### Environment and limits

Copy `.env.example` to the deployment secrets file and fill in the required
credentials. Video review settings are:

- `VIDEO_MEDIA_ROOT`: owner-only local storage, defaulting to
  `data/video-media` in development. The systemd deployment uses
  `/var/peermarket-agent/video-media`.
- `VIDEO_MAX_FILE_BYTES`: maximum downloaded file size (default 200 MiB).
- `VIDEO_MAX_CLIPS`: maximum clips combined for one draft (default 8).
- `VIDEO_MAX_DURATION_SECONDS`: maximum duration per clip (default 60 seconds).
- `VIDEO_RETENTION_DAYS`: operational retention target (default 30 days).

The service creates the configured media root with mode `0700`, owned by
`peermarket-agent`, and refuses to start if `ffmpeg` or `ffprobe` is absent.
Keep a configured media root below `/var/peermarket-agent` so systemd's
write restriction continues to protect the host.

Retention cleanup is manual and should be performed after confirming that no
active review needs the files, for example by removing old `draft-*` folders
under `VIDEO_MEDIA_ROOT`. Do not add an automatic publisher or automatic
cleanup job without an explicit operational decision and review.

### Founder upload workflow

1. Wait for the agent's TikTok draft message in Slack.
2. Reply in that exact thread with one or more portrait videos (`.mp4`, `.mov`,
   or `.webm`, matching the MIME type).
3. The bridge acknowledges the upload asynchronously, validates and reviews
   it in the background, and replies in the thread with the result.
4. Review the result and use the explicit approval/rejection workflow. Uploads
   from anyone other than `SLACK_FOUNDER_USER_ID`, or uploads outside an active
   TikTok draft thread, are rejected.
