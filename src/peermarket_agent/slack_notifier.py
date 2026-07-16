"""Slack outbound notifier — DMs the founder when something needs attention."""

import structlog
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)


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

    async def post_draft_thread(self, draft_id: int, text: str) -> tuple[str, str]:
        """Post a draft's root message and return its Slack thread reference."""
        response = await self._client.chat_postMessage(
            channel=self._founder_user_id,
            text=text,
        )
        channel_id = response["channel"]
        message_ts = response["ts"]
        log.info(
            "slack_notifier.draft_thread_posted",
            draft_id=draft_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        return channel_id, message_ts

    async def post_thread_reply(self, channel_id: str, thread_ts: str, text: str) -> bool:
        """Reply to a persisted draft thread without opening a new founder DM."""
        try:
            await self._client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=text,
            )
            return True
        except Exception:
            log.exception(
                "slack_notifier.thread_reply_failed", channel_id=channel_id, thread_ts=thread_ts
            )
            return False
