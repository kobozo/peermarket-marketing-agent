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
