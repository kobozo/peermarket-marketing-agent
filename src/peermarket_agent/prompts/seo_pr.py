"""SEO PR — meta tag generator. Produces <title> + <meta description> for a page."""

import json
import math
from dataclasses import dataclass

from peermarket_agent.claude import ClaudeClient, ClaudeResponse

_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015


def _cost_cents(resp: ClaudeResponse) -> int:
    raw = resp.input_tokens * _INPUT_CENTS_PER_TOKEN + resp.output_tokens * _OUTPUT_CENTS_PER_TOKEN
    return max(1, math.ceil(raw))


def _system_prompt(brand_voice_md: str) -> str:
    return (
        "You are PeerMarket's SEO copywriter.\n\n"
        "Brand voice (READ FIRST):\n"
        "----\n"
        f"{brand_voice_md}\n"
        "----\n\n"
        "Your job: write the <title> tag and <meta description> for a page.\n\n"
        "Output format: a JSON object with exactly two string fields:\n"
        "{\n"
        '  "title": "<≤60 chars>",\n'
        '  "description": "<50-160 chars>"\n'
        "}\n\n"
        "Hard constraints:\n"
        "- Title ≤ 60 chars, ends with brand name pattern (`| PeerMarket` or `— PeerMarket`).\n"
        "- Description 50-160 chars, includes one search-intent keyword for the page.\n"
        "- Match the language exactly. No emojis.\n"
    )


@dataclass(frozen=True)
class SeoMeta:
    title: str
    description: str
    cost_cents: int


async def generate_seo_meta(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    language: str,
    page_path: str,
    page_subject: str,
) -> SeoMeta:
    user = f"Language: {language}\nPage path: {page_path}\nPage subject: {page_subject}\n"
    resp = await claude.complete(
        system=_system_prompt(brand_voice_md),
        user=user,
        temperature=0.4,
        max_tokens=300,
    )
    try:
        payload = json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned not valid JSON: {resp.text[:200]!r}") from e
    title = payload["title"]
    description = payload["description"]
    if len(title) > 60:
        raise ValueError(f"title too long ({len(title)} > 60): {title!r}")
    if not (50 <= len(description) <= 160):
        raise ValueError(
            f"description length out of range ({len(description)} not in 50-160): {description!r}"
        )
    return SeoMeta(title=title, description=description, cost_cents=_cost_cents(resp))
