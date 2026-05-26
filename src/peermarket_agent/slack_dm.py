"""Slack DM formatting for drafts.

Plain-text mrkdwn formatting (Slack's lightweight markdown). Full Block-Kit
buttons land in Phase 1b proper. For now: human readable, ID + brand score
prominent, clear approve/reject instructions.
"""

from typing import TypedDict


class DraftDict(TypedDict):
    id: int
    action_type_name: str
    language: str
    channel: str
    brand_score: int
    copy: str


_HEADER_LABELS: dict[str, tuple[str, str]] = {
    "tiktok_post_organic": ("🎬", "TikTok organic"),
    "email_re_engagement": ("✉️", "Email re-engagement"),
    "seo_pr": ("🔍", "SEO PR meta-tag"),
    "meta_ad_creative": ("📣", "Meta ad"),
}


def format_draft_dm(draft: DraftDict) -> str:
    """Format a persisted draft row as a plain-text Slack DM.

    The dict is a raw row pulled from the ``drafts`` table joined with
    ``action_types``, NOT a ``Draft`` dataclass.
    """
    emoji, label = _HEADER_LABELS.get(
        draft["action_type_name"],
        ("📝", draft["action_type_name"]),
    )
    header = (
        f"{emoji} *{label} draft #{draft['id']}* — score {draft['brand_score']}\n"
        f"{draft['language']} · channel: {draft['channel']}"
    )
    ack = f"\n\nReply *✅ {draft['id']}* to approve, *❌ {draft['id']}* to reject."
    return f"{header}\n\n{draft['copy']}{ack}"


def format_summary_dm(*, drafts_persisted: int, drafts_attempted: int) -> str:
    """Morning summary DM after Loop B finishes."""
    return (
        f"Goedemorgen ☕ — vandaag heb ik {drafts_persisted}/{drafts_attempted} drafts klaar. "
        f"Tap een ✅ of ❌ in de vorige berichten, of typ `✅ <id>` / `❌ <id>` "
        f"als je liever zo werkt."
    )
