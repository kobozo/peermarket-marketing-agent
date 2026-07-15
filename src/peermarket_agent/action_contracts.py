"""Canonical structured-output validation shared by generation and revision."""

CTA_LABELS = {"Learn More", "Sign Up", "Shop Now", "Get Started"}


def _strings(payload: dict, fields: set[str], label: str, *, forbid_em_dash: bool = True) -> None:
    if not all(isinstance(payload.get(field), str) for field in fields):
        raise ValueError(f"{label} fields must be strings")
    if forbid_em_dash and any("—" in payload[field] for field in fields):
        raise ValueError(f"{label} fields cannot contain em-dashes")


def validate_tiktok(payload: dict) -> None:
    fields = {"hook", "body", "cta"}
    _strings(payload, fields, "TikTok")
    hook_words = len(payload["hook"].split())
    body_words = len(payload["body"].split())
    cta_words = len(payload["cta"].split())
    if not 8 <= hook_words <= 12 or payload["hook"].endswith("!"):
        raise ValueError("TikTok hook must be 8-12 words and never end with an exclamation")
    if body_words > 30:
        raise ValueError("TikTok body must be at most 30 words")
    if not 3 <= cta_words <= 6:
        raise ValueError("TikTok CTA must be 3-6 words")
    if hook_words + body_words + cta_words > 50:
        raise ValueError("TikTok copy must be at most 50 words")


def validate_email(payload: dict) -> None:
    _strings(payload, {"subject", "body"}, "email")
    if len(payload["subject"]) > 60:
        raise ValueError("email subject must be at most 60 characters")
    words = len(payload["body"].split())
    if not 80 <= words <= 180:
        raise ValueError("email body must be 80-180 words")
    if "<" in payload["body"] or ">" in payload["body"]:
        raise ValueError("email body must be plain text")


def validate_seo(payload: dict) -> None:
    _strings(payload, {"title", "description"}, "SEO", forbid_em_dash=False)
    if len(payload["title"]) > 60:
        raise ValueError("SEO title must be at most 60 characters")
    if not 50 <= len(payload["description"]) <= 160:
        raise ValueError("SEO description must be 50-160 characters")
    if not payload["title"].endswith(("| PeerMarket", "— PeerMarket")):
        raise ValueError("SEO title must end with PeerMarket brand pattern")


def validate_meta(payload: dict, *, allowed_audiences: set[str]) -> None:
    text_fields = {"primary_text", "headline", "description", "cta_label", "audience_profile_key"}
    _strings(payload, text_fields, "Meta")
    if not 125 <= len(payload["primary_text"]) <= 300:
        raise ValueError("Meta primary_text must be 125-300 characters")
    if len(payload["headline"]) > 40 or "!" in payload["headline"]:
        raise ValueError("Meta headline must be at most 40 characters without exclamation")
    if len(payload["description"]) > 40:
        raise ValueError("Meta description must be at most 40 characters")
    if payload["cta_label"] not in CTA_LABELS:
        raise ValueError("Meta cta_label is not allowed")
    budget = payload.get("suggested_daily_budget_eur")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 5 <= budget <= 20:
        raise ValueError("Meta suggested_daily_budget_eur must be an integer from 5-20")
    if payload["audience_profile_key"] not in allowed_audiences:
        raise ValueError("Meta audience is not allowed")
