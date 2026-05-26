"""Email re-engagement — prompt builders + generator."""

import math
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.claude import ClaudeClient, ClaudeResponse

_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015


def _cost_cents(resp: ClaudeResponse) -> int:
    raw = resp.input_tokens * _INPUT_CENTS_PER_TOKEN + resp.output_tokens * _OUTPUT_CENTS_PER_TOKEN
    return max(1, math.ceil(raw))


def build_system_prompt(brand_voice_md: str) -> str:
    return (
        "You are PeerMarket's lifecycle email writer.\n\n"
        "Brand voice (READ FIRST):\n"
        "----\n"
        f"{brand_voice_md}\n"
        "----\n\n"
        "Your job: write one re-engagement email in the requested language.\n\n"
        "Output format: a JSON object with exactly two string fields:\n"
        "{\n"
        '  "subject": "<≤60 chars>",\n'
        '  "body": "<plain text email body, 80-180 words>"\n'
        "}\n\n"
        "Hard constraints:\n"
        "- Subject ≤ 60 chars, no emojis, no excess punctuation.\n"
        "- Body is plain text, no HTML tags, no salesy hype.\n"
        "- One clear call to action in the body.\n"
        "- Match the language exactly.\n"
    )


def build_user_prompt(*, language: str, audience: str) -> str:
    audience_descriptions = {
        "dormant_signups": ("Users who signed up >7 days ago but have never published a listing."),
    }
    desc = audience_descriptions.get(audience, audience)
    return (
        f"Language: {language}\n"
        f"Audience: {audience} — {desc}\n"
        "Tone: gentle nudge, not pressure. Lean into trust + ease of listing.\n"
    )


@dataclass(frozen=True)
class Email:
    subject: str
    body: str
    cost_cents: int


async def generate_email(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    language: str,
    audience: str,
) -> Email:
    resp = await claude.complete(
        system=build_system_prompt(brand_voice_md),
        user=build_user_prompt(language=language, audience=audience),
        temperature=0.7,
        max_tokens=600,
    )
    payload = parse_claude_json(resp.text)
    subject = payload["subject"]
    if len(subject) > 60:
        raise ValueError(f"subject too long ({len(subject)} > 60): {subject!r}")
    return Email(
        subject=subject,
        body=payload["body"],
        cost_cents=_cost_cents(resp),
    )
