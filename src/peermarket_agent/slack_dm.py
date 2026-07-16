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
    script: str
    shots: list[str]
    on_screen_text: list[str]
    recording_notes: str


class RevisedDraftDict(DraftDict):
    revision_number: int
    revision_feedback: str


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
    if draft["action_type_name"] != "tiktok_post_organic":
        return f"{header}\n\n{draft['copy']}{ack}"

    shots = draft.get("shots") or []
    overlays = draft.get("on_screen_text") or []
    brief = (
        "\n\n*Recording brief*\n"
        f"*Spoken script:* {draft.get('script', '')}\n"
        f"*Shots:* {'; '.join(shots)}\n"
        f"*On-screen text:* {'; '.join(overlays)}\n"
        f"*Recording notes:* {draft.get('recording_notes', '')}\n\n"
        "After approval, reply with one or more videos in this Slack thread."
    )
    return f"{header}\n\n{draft['copy']}{brief}{ack}"


def format_summary_dm(*, drafts_persisted: int, drafts_attempted: int) -> str:
    """Morning summary DM after Loop B finishes."""
    return (
        f"Goedemorgen ☕ — vandaag heb ik {drafts_persisted}/{drafts_attempted} drafts klaar. "
        f"Tap een ✅ of ❌ in de vorige berichten, of typ `✅ <id>` / `❌ <id>` "
        f"als je liever zo werkt."
    )


def format_revised_draft_dm(draft: RevisedDraftDict, *, change_summary: str) -> str:
    """Format a complete replacement variant for its existing approval thread."""
    base = format_draft_dm(draft)
    header, copy_and_ack = base.split("\n\n", 1)
    return (
        f"{header} · revision {draft['revision_number']}\n\n"
        f"*Changes applied:* {change_summary}\n\n{copy_and_ack}"
    )
