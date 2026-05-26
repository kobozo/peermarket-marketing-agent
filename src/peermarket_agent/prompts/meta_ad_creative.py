"""Meta Ad creative — prompt builders + generator.

Output is a draft of a Meta Ads Manager creative: primary text + headline +
description + CTA + suggested daily budget. Audience profile is selected
by the caller (or randomly rotated by the daily loop). No Meta API calls
in this module — this is draft-only.
"""

import math
import random
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.claude import ClaudeClient, ClaudeResponse

_INPUT_CENTS_PER_TOKEN = 0.0003
_OUTPUT_CENTS_PER_TOKEN = 0.0015

_ALLOWED_CTA_LABELS = ("Learn More", "Sign Up", "Shop Now", "Get Started")

AUDIENCE_PROFILES: dict[str, dict] = {
    "declutterers": {
        "label": "Declutterers — parents 28-55, Belgium NL+FR",
        "age_min": 28,
        "age_max": 55,
        "interests": ["Parenting", "Home decor", "Secondhand shopping", "Sustainable living"],
        "geo": "Belgium",
        "languages": ["NL", "FR"],
        "rationale": "Persona A — volume play, broad reachable audience",
    },
    "trust_conscious_locals": {
        "label": "Trust-conscious locals — 35-65, Belgium NL+FR, anti-scam",
        "age_min": 35,
        "age_max": 65,
        "interests": [
            "Online safety",
            "Local marketplaces",
            "Marktplaats alternatives",
            "Identity verification",
        ],
        "geo": "Belgium",
        "languages": ["NL", "FR"],
        "rationale": "Persona D wedge — narrower but better fit for verified-identity positioning",
    },
}


def _cost_cents(resp: ClaudeResponse) -> int:
    raw = resp.input_tokens * _INPUT_CENTS_PER_TOKEN + resp.output_tokens * _OUTPUT_CENTS_PER_TOKEN
    return max(1, math.ceil(raw))


def pick_audience(rng: random.Random | None = None) -> str:
    """Return a random audience profile key. Caller can pin via rng for tests."""
    r = rng or random.Random()
    return r.choice(list(AUDIENCE_PROFILES.keys()))


def build_system_prompt(brand_voice_md: str) -> str:
    return (
        "You are PeerMarket's Meta Ads creative writer.\n\n"
        "Brand voice (READ FIRST):\n"
        "----\n"
        f"{brand_voice_md}\n"
        "----\n\n"
        "Your job: write one Meta Ads creative in the requested language for the given audience.\n\n"
        "Output format: a JSON object with exactly five fields, no markdown, no commentary:\n"
        "{\n"
        '  "primary_text": "<125-300 chars main ad body>",\n'
        '  "headline": "<≤40 chars>",\n'
        '  "description": "<≤30 chars>",\n'
        '  "cta_label": "<one of: Learn More | Sign Up | Shop Now | Get Started>",\n'
        '  "suggested_daily_budget_eur": <integer 5-20>\n'
        "}\n\n"
        "Hard constraints:\n"
        "- primary_text 125-300 chars, plain text, no emojis except optionally one in the opener\n"
        "- headline ≤ 40 chars, no exclamation mark\n"
        "- description ≤ 30 chars (Meta's short subtitle)\n"
        "- cta_label exactly one of the four allowed values\n"
        "- suggested_daily_budget_eur 5-20 integer (we're testing — start small)\n"
        "- Stay strictly in the requested language; never mix\n"
        "- No em-dashes anywhere\n"
    )


def build_user_prompt(
    *,
    language: str,
    audience_profile_key: str,
) -> str:
    profile = AUDIENCE_PROFILES[audience_profile_key]
    return (
        f"Language: {language}\n"
        f"Audience profile: {profile['label']}\n"
        f"Age range: {profile['age_min']}-{profile['age_max']}\n"
        f"Geo: {profile['geo']}\n"
        f"Interests: {', '.join(profile['interests'])}\n"
        f"Rationale: {profile['rationale']}\n\n"
        "Angle: trust + verified identity wedge. Counter to Marktplaats/Facebook Marketplace scams.\n"
    )


@dataclass(frozen=True)
class MetaAdCreative:
    primary_text: str
    headline: str
    description: str
    cta_label: str
    suggested_daily_budget_eur: int
    audience_profile_key: str
    cost_cents: int


async def generate_meta_ad_creative(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    language: str,
    audience_profile_key: str,
) -> MetaAdCreative:
    if audience_profile_key not in AUDIENCE_PROFILES:
        raise ValueError(
            f"unknown audience profile: {audience_profile_key!r} "
            f"(valid: {list(AUDIENCE_PROFILES.keys())})"
        )
    resp = await claude.complete(
        system=build_system_prompt(brand_voice_md),
        user=build_user_prompt(
            language=language,
            audience_profile_key=audience_profile_key,
        ),
        temperature=0.7,
        max_tokens=600,
    )
    payload = parse_claude_json(resp.text)
    primary_text = payload["primary_text"]
    headline = payload["headline"]
    description = payload["description"]
    cta_label = payload["cta_label"]
    budget = int(payload["suggested_daily_budget_eur"])

    if not (125 <= len(primary_text) <= 300):
        raise ValueError(
            f"primary_text length out of range ({len(primary_text)} not in 125-300): "
            f"{primary_text!r}"
        )
    if len(headline) > 40:
        raise ValueError(f"headline too long ({len(headline)} > 40): {headline!r}")
    if len(description) > 30:
        raise ValueError(f"description too long ({len(description)} > 30): {description!r}")
    if cta_label not in _ALLOWED_CTA_LABELS:
        raise ValueError(
            f"cta_label not allowed: {cta_label!r} (must be one of {_ALLOWED_CTA_LABELS})"
        )
    if not (5 <= budget <= 20):
        raise ValueError(f"suggested_daily_budget_eur out of range ({budget} not in 5-20)")

    return MetaAdCreative(
        primary_text=primary_text,
        headline=headline,
        description=description,
        cta_label=cta_label,
        suggested_daily_budget_eur=budget,
        audience_profile_key=audience_profile_key,
        cost_cents=_cost_cents(resp),
    )
