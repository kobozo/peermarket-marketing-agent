"""Slack DM formatter tests."""

from peermarket_agent.slack_dm import (
    format_draft_dm,
    format_revised_draft_dm,
    format_summary_dm,
)

_BASE_DRAFT = {
    "id": 42,
    "language": "NL",
    "channel": "tiktok",
    "brand_score": 88,
    "copy": "Marktplaats moe? Verkoop veilig op PeerMarket.",
    "script": "Marktplaats moe? Verkoop veilig op PeerMarket. Plaats nu.",
    "shots": ["Praat in camera", "Toon telefoon"],
    "on_screen_text": ["Verkoop veilig"],
    "recording_notes": "Film verticaal bij daglicht.",
}


def test_format_tiktok_draft():
    msg = format_draft_dm({**_BASE_DRAFT, "action_type_name": "tiktok_post_organic"})
    assert "TikTok organic" in msg
    assert "#42" in msg
    assert "score 88" in msg
    assert "Marktplaats moe?" in msg
    assert "✅ 42" in msg
    assert "❌ 42" in msg
    assert "Recording brief" in msg
    assert "Marktplaats moe? Verkoop veilig op PeerMarket. Plaats nu." in msg
    assert "Praat in camera" in msg
    assert "Verkoop veilig" in msg
    assert "Film verticaal bij daglicht." in msg
    assert "reply with one or more videos in this Slack thread" in msg


def test_format_meta_draft():
    msg = format_draft_dm(
        {**_BASE_DRAFT, "action_type_name": "meta_ad_creative", "channel": "meta"}
    )
    assert "📣" in msg
    assert "Meta ad" in msg


def test_format_email_draft():
    msg = format_draft_dm(
        {**_BASE_DRAFT, "action_type_name": "email_re_engagement", "channel": "email"}
    )
    assert "✉️" in msg
    assert "Email re-engagement" in msg


def test_format_unknown_action_type_falls_back():
    msg = format_draft_dm({**_BASE_DRAFT, "action_type_name": "future_action"})
    assert "future_action" in msg  # fallback shows the raw name
    assert "📝" in msg


def test_format_summary_dm():
    msg = format_summary_dm(drafts_persisted=2, drafts_attempted=3)
    assert "2/3" in msg
    assert "Goedemorgen" in msg


def test_format_revised_draft_has_complete_copy_summary_and_new_decision_id():
    msg = format_revised_draft_dm(
        {
            **_BASE_DRAFT,
            "id": 43,
            "action_type_name": "tiktok_post_organic",
            "revision_number": 1,
            "revision_feedback": "Make the CTA warmer",
        },
        change_summary="Warmer CTA while preserving the offer",
    )
    assert "draft #43" in msg
    assert "revision 1" in msg
    assert "Marktplaats moe?" in msg
    assert "Warmer CTA while preserving the offer" in msg
    assert "✅ 43" in msg
    assert "❌ 43" in msg
