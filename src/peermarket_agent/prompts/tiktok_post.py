"""TikTok organic post — prompt builders + generator."""

import math
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.action_contracts import validate_tiktok
from peermarket_agent.claude import ClaudeClient, ClaudeResponse

# Sonnet 4.6 approximate pricing: $3/M input, $15/M output → cents per token.
# 1 token input  ≈ 0.0003 cents
# 1 token output ≈ 0.0015 cents
_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015


def _cost_cents(resp: ClaudeResponse) -> int:
    raw = resp.input_tokens * _INPUT_CENTS_PER_TOKEN + resp.output_tokens * _OUTPUT_CENTS_PER_TOKEN
    return max(1, math.ceil(raw))  # always at least 1 cent per call


def build_system_prompt(brand_voice_md: str) -> str:
    return (
        "You are PeerMarket's marketing-content writer for TikTok organic posts.\n\n"
        "Anchored brand voice (READ FIRST):\n"
        "----\n"
        f"{brand_voice_md}\n"
        "----\n\n"
        "Your job: write one TikTok organic post in the requested language.\n\n"
        "Output format: a JSON object with exactly three string fields, "
        "no markdown, no commentary:\n"
        "{\n"
        '  "hook": "<8-12 word punch opener>",\n'
        '  "body": "<1-2 sentence middle, ≤30 words>",\n'
        '  "cta": "<3-6 word call to action>"\n'
        "}\n\n"
        "Hard constraints:\n"
        "- Hook ends with a question mark or a short statement, never an exclamation\n"
        "- No em-dashes (—). Commas only.\n"
        "- Stay strictly in the requested language; never mix.\n"
        "- Hook + body + cta ≤ 50 words total.\n"
    )


def build_user_prompt(*, language: str, theme: str) -> str:
    return (
        f"Language: {language}\n"
        f"Theme: {theme}\n"
        "Audience: Belgian declutterers (parents, mid-30s to 50s).\n"
        "Angle: trust-conscious, anti-scam, verified-identity wedge.\n"
    )


@dataclass(frozen=True)
class TikTokPost:
    hook: str
    body: str
    cta: str
    cost_cents: int


async def generate_tiktok_post(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    language: str,
    theme: str,
) -> TikTokPost:
    resp = await claude.complete(
        system=build_system_prompt(brand_voice_md),
        user=build_user_prompt(language=language, theme=theme),
        temperature=0.7,
        max_tokens=400,
    )
    payload = parse_claude_json(resp.text)
    validate_tiktok(payload)
    return TikTokPost(
        hook=payload["hook"],
        body=payload["body"],
        cta=payload["cta"],
        cost_cents=_cost_cents(resp),
    )
