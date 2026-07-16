"""Generate and validate immutable replacement draft variants."""

import math
import re
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.action_contracts import (
    validate_email,
    validate_meta,
    validate_seo,
    validate_tiktok,
)
from peermarket_agent.agent.cli_draft import _human_cta_to_meta_enum
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.drafts import Draft, Language
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.prompts.draft_revision import build_revision_prompts
from peermarket_agent.prompts.meta_ad_creative import AUDIENCE_PROFILES

_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015


@dataclass(frozen=True)
class SourceDraft:
    action_type_name: str
    channel: str
    language: Language
    copy: str
    metadata: dict
    asset_path: str | None
    revision_number: int = 0


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


_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|niet|geen|nooit|ne\b.*\bpas|n['’].*\bpas)\b",
    re.IGNORECASE,
)
_UNTRUSTED_INTENT_RE = re.compile(r"\b(?:ignore|instructions?|output|source|old copy)\b", re.I)
_POLITE = r"(?:please\s+|can you\s+|could you\s+|wil je\s+|kun je\s+|graag\s+|merci de\s+|peux-tu\s+|pouvez-vous\s+)?"
_INTENT_PATTERNS = {
    "suggested_daily_budget_eur": re.compile(
        rf"^{_POLITE}(?:increase|decrease|raise|lower|change|set|verhoog|verlaag|wijzig|pas|augmente|diminue|modifie|change)\b.*\b(?:budget|spend|euro|euros)\b",
        re.I,
    ),
    "cta_label": re.compile(
        rf"^{_POLITE}(?:change|set|replace|wijzig|verander|pas|modifie|change|remplace)\b.*\b(?:cta|call to action|appel)\b",
        re.I,
    ),
    "audience_profile_key": re.compile(
        rf"^{_POLITE}(?:change|set|replace|target|wijzig|verander|richt|modifie|change|cible)\b.*\b(?:audience|doelgroep|public|cible)\b",
        re.I,
    ),
}


def classify_protected_intent(feedback: tuple[str, ...]) -> set[str]:
    """Recognize only direct affirmative protected-field edit commands."""
    intents: set[str] = set()
    for instruction in feedback:
        normalized = " ".join(instruction.strip().split())
        if _NEGATION_RE.search(normalized) or _UNTRUSTED_INTENT_RE.search(normalized):
            continue
        for field, pattern in _INTENT_PATTERNS.items():
            if pattern.search(normalized):
                intents.add(field)
    return intents


def _format_payload(source: SourceDraft, payload: dict) -> tuple[str, dict]:
    action = source.action_type_name
    if action == "tiktok_post_organic":
        fields = {"hook", "body", "cta"}
        _exact(payload, fields)
        validate_tiktok(payload)
        metadata = dict(source.metadata)
        metadata.update({key: payload[key] for key in fields})
        return f"{payload['hook']}\n\n{payload['body']}\n\n{payload['cta']}", metadata
    if action == "email_re_engagement":
        fields = {"subject", "body"}
        _exact(payload, fields)
        validate_email(payload)
        metadata = dict(source.metadata)
        metadata.update({key: payload[key] for key in fields})
        return f"Subject: {payload['subject']}\n\n{payload['body']}", metadata
    if action == "seo_pr":
        fields = {"title", "description"}
        _exact(payload, fields)
        validate_seo(payload)
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
    validate_meta(payload, allowed_audiences=set(AUDIENCE_PROFILES))
    budget = payload["suggested_daily_budget_eur"]
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
    protected_intent = classify_protected_intent(feedback)
    if (
        source_draft.action_type_name == "tiktok_post_organic"
        and "cta" in source_draft.metadata
        and metadata["cta"] != source_draft.metadata["cta"]
        and "cta_label" not in protected_intent
    ):
        raise ValueError("unrequested protected field change: cta")
    if source_draft.action_type_name == "meta_ad_creative":
        protected = {
            "audience_profile_key",
            "cta_label",
            "suggested_daily_budget_eur",
        }
        for field in protected:
            if (
                metadata.get(field) != source_draft.metadata.get(field)
                and field not in protected_intent
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
