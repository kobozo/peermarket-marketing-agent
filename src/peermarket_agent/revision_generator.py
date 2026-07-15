"""Generate and validate immutable replacement draft variants."""

import math
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.agent.cli_draft import _human_cta_to_meta_enum
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.drafts import Draft, Language
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.prompts.draft_revision import build_revision_prompts

_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015
_CTA_LABELS = {"Learn More", "Sign Up", "Shop Now", "Get Started"}


@dataclass(frozen=True)
class SourceDraft:
    action_type_name: str
    channel: str
    language: Language
    copy: str
    metadata: dict
    asset_path: str | None


@dataclass(frozen=True)
class RevisedDraft:
    draft: Draft
    change_summary: str
    status: str = "queued"


def _exact(payload: dict, fields: set[str]) -> None:
    if set(payload) != fields | {"change_summary"}:
        raise ValueError(
            f"revision output must contain exactly {sorted(fields | {'change_summary'})}"
        )
    if not isinstance(payload["change_summary"], str) or not payload["change_summary"].strip():
        raise ValueError("change_summary must be a non-empty string")


def _requested(feedback: tuple[str, ...], *words: str) -> bool:
    combined = " ".join(feedback).casefold()
    return any(word.casefold() in combined for word in words)


def _format_payload(source: SourceDraft, payload: dict) -> tuple[str, dict]:
    action = source.action_type_name
    if action == "tiktok_post_organic":
        fields = {"hook", "body", "cta"}
        _exact(payload, fields)
        if not all(isinstance(payload[key], str) for key in fields):
            raise ValueError("TikTok fields must be strings")
        metadata = dict(source.metadata)
        metadata.update({key: payload[key] for key in fields})
        return f"{payload['hook']}\n\n{payload['body']}\n\n{payload['cta']}", metadata
    if action == "email_re_engagement":
        fields = {"subject", "body"}
        _exact(payload, fields)
        if not all(isinstance(payload[key], str) for key in fields):
            raise ValueError("email fields must be strings")
        if len(payload["subject"]) > 60:
            raise ValueError("subject too long")
        metadata = dict(source.metadata)
        metadata.update({key: payload[key] for key in fields})
        return f"Subject: {payload['subject']}\n\n{payload['body']}", metadata
    if action == "seo_pr":
        fields = {"title", "description"}
        _exact(payload, fields)
        if not all(isinstance(payload[key], str) for key in fields):
            raise ValueError("SEO fields must be strings")
        if len(payload["title"]) > 60 or not 50 <= len(payload["description"]) <= 160:
            raise ValueError("SEO field length out of range")
        copy = f'<title>{payload["title"]}</title>\n<meta name="description" content="{payload["description"]}">'
        metadata = dict(source.metadata)
        metadata.update({key: payload[key] for key in fields})
        return copy, metadata
    if action != "meta_ad_creative":
        raise ValueError(f"unsupported action_type: {action!r}")
    fields = {
        "primary_text",
        "headline",
        "description",
        "cta_label",
        "suggested_daily_budget_eur",
        "audience_profile_key",
    }
    _exact(payload, fields)
    if not all(isinstance(payload[key], str) for key in fields - {"suggested_daily_budget_eur"}):
        raise ValueError("Meta text fields must be strings")
    budget = payload["suggested_daily_budget_eur"]
    if isinstance(budget, bool) or not isinstance(budget, int) or not 5 <= budget <= 20:
        raise ValueError("Meta budget out of range")
    if (
        not 125 <= len(payload["primary_text"]) <= 300
        or len(payload["headline"]) > 40
        or len(payload["description"]) > 40
    ):
        raise ValueError("Meta field length out of range")
    if payload["cta_label"] not in _CTA_LABELS:
        raise ValueError("Meta CTA is not allowed")
    metadata = dict(source.metadata)
    metadata.update({key: payload[key] for key in fields})
    metadata["cta_type"] = _human_cta_to_meta_enum(payload["cta_label"])
    copy = (
        f"Audience: {payload['audience_profile_key']}\nHeadline: {payload['headline']}\n"
        f"Description: {payload['description']}\nCTA: {payload['cta_label']}\n"
        f"Suggested daily budget: €{budget}\n\nPrimary text:\n{payload['primary_text']}"
    )
    return copy, metadata


async def revise_draft(
    claude: ClaudeClient, source_draft: SourceDraft, feedback: tuple[str, ...]
) -> RevisedDraft:
    if not feedback:
        raise ValueError("revision feedback cannot be empty")
    system, user = build_revision_prompts(
        brand_voice_md=load_brand_voice(),
        action_type_name=source_draft.action_type_name,
        language=source_draft.language,
        source_copy=source_draft.copy,
        source_metadata=source_draft.metadata,
        feedback=feedback,
    )
    response = await claude.complete(system=system, user=user, temperature=0.2, max_tokens=1000)
    payload = parse_claude_json(response.text)
    copy, metadata = _format_payload(source_draft, payload)
    if (
        source_draft.action_type_name == "tiktok_post_organic"
        and "cta" in source_draft.metadata
        and metadata["cta"] != source_draft.metadata["cta"]
        and not _requested(feedback, "cta", "call to action", "oproep", "appel")
    ):
        raise ValueError("unrequested protected field change: cta")
    if source_draft.action_type_name == "meta_ad_creative":
        protected = {
            "audience_profile_key": ("audience", "doelgroep", "public"),
            "cta_label": ("cta", "call to action", "appel"),
            "suggested_daily_budget_eur": ("budget", "spend", "euro", "€"),
        }
        for field, keywords in protected.items():
            if metadata.get(field) != source_draft.metadata.get(field) and not _requested(
                feedback, *keywords
            ):
                raise ValueError(f"unrequested protected field change: {field}")
    cost = max(
        1,
        math.ceil(
            response.input_tokens * _INPUT_CENTS_PER_TOKEN
            + response.output_tokens * _OUTPUT_CENTS_PER_TOKEN
        ),
    )
    draft = Draft(
        source_draft.action_type_name,
        source_draft.channel,
        source_draft.language,
        copy,
        source_draft.asset_path,
        cost,
        0,
        True,
        metadata,
    )
    return RevisedDraft(draft=draft, change_summary=payload["change_summary"].strip())
