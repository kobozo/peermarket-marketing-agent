"""Slack outbound notifier — DMs the founder when something needs attention."""

from dataclasses import dataclass

import structlog
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SlackMessageResult:
    channel_id: str
    ts: str


class SlackNotifier:
    def __init__(self, *, bot_token: str, founder_user_id: str) -> None:
        self._client = AsyncWebClient(token=bot_token)
        self._founder_user_id = founder_user_id

    async def notify_founder(self, text: str) -> bool:
        """DM the founder. Returns True if sent, False if no founder configured."""
        if not self._founder_user_id:
            log.warning(
                "slack_notifier.no_founder_id",
                hint="Set SLACK_FOUNDER_USER_ID secret to enable founder DMs",
            )
            return False
        try:
            await self._client.chat_postMessage(
                channel=self._founder_user_id,
                text=text,
            )
            return True
        except Exception:
            log.exception("slack_notifier.send_failed")
            return False

    async def send_message(
        self,
        text: str,
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
    ) -> SlackMessageResult:
        """Post a message and expose Slack's authoritative message identity.

        Unlike ``notify_founder``, this delivery interface propagates failures so
        durable callers can record and retry them.
        """
        channel = channel_id or self._founder_user_id
        if not channel:
            raise ValueError("Slack founder/channel ID is not configured")
        kwargs = {"channel": channel, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        response = await self._client.chat_postMessage(**kwargs)
        return SlackMessageResult(
            channel_id=str(response["channel"]),
            ts=str(response["ts"]),
        )
