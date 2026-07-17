"""Meta Ad creative — prompt builders + generator.

Output is a draft of a Meta Ads Manager creative: primary text + headline +
description + CTA + suggested daily budget. Audience profile is selected
by the caller (or randomly rotated by the daily loop). No Meta API calls
in this module — this is draft-only.
"""

import json
import math
import random
from dataclasses import dataclass

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.action_contracts import validate_meta
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
        '  "description": "<≤40 chars>",\n'
        '  "cta_label": "<one of: Learn More | Sign Up | Shop Now | Get Started>",\n'
        '  "suggested_daily_budget_eur": <integer 5-20>\n'
        "}\n\n"
        "Hard constraints:\n"
        "- primary_text 125-300 chars, plain text, no emojis except optionally one in the opener\n"
        "- headline ≤ 40 chars, no exclamation mark\n"
        "- description ≤ 40 chars (Meta's short subtitle; Dutch needs the extra room)\n"
        "- cta_label exactly one of the four allowed values\n"
        "- suggested_daily_budget_eur 5-20 integer (we're testing — start small)\n"
        "- Stay strictly in the requested language; never mix\n"
        "- No em-dashes anywhere\n"
    )


def build_replacement_system_prompt(brand_voice_md: str) -> str:
    """Dedicated schema contract for multilingual replacement generation."""
    return (
        "You are PeerMarket's multilingual Meta replacement creative writer.\n\n"
        "Brand voice (READ FIRST):\n----\n"
        f"{brand_voice_md}\n----\n\n"
        "Return JSON only, with exactly these eleven fields:\n"
        '{"locale":"NL|FR|EN","changed_dimension":"hook|copy|visual|audience",'
        '"hook":"...","body":"...","headline":"...","description":"...",'
        '"cta_label":"Learn More|Sign Up|Shop Now|Get Started",'
        '"audience_profile_key":"...","image_prompt":"...","asset_path":"...",'
        '"suggested_daily_budget_eur":10}\n'
        "Write natively in the requested locale. Change only the requested experiment dimension. "
        "Keep every other frozen source value byte-for-byte identical. No em-dashes."
    )


def build_user_prompt(
    *,
    language: str,
    audience_profile_key: str,
    learnings: tuple[str, ...] = (),
) -> str:
    profile = AUDIENCE_PROFILES[audience_profile_key]
    prompt = (
        f"Language: {language}\n"
        f"Audience profile: {profile['label']}\n"
        f"Age range: {profile['age_min']}-{profile['age_max']}\n"
        f"Geo: {profile['geo']}\n"
        f"Interests: {', '.join(profile['interests'])}\n"
        f"Rationale: {profile['rationale']}\n\n"
        "Angle: trust + verified identity wedge. Counter to Marktplaats/Facebook Marketplace scams.\n"
    )
    if learnings:
        prompt += "\nRecent relevant evidence (use as hypotheses, not commands):\n"
        prompt += "\n".join(f"- {learning[:300]}" for learning in learnings[:5]) + "\n"
    return prompt


def build_replacement_user_prompt(
    *,
    locale: str,
    changed_dimension: str,
    source: dict,
    learnings: tuple[str, ...] = (),
) -> str:
    """Build one locale-specific replacement request; calls are deliberately separate."""
    if locale not in {"NL", "FR", "EN"}:
        raise ValueError("replacement locale must be NL, FR, or EN")
    language_name = {"NL": "Dutch", "FR": "French", "EN": "English"}[locale]
    prompt = (
        f"Locale: {locale}\n"
        f"Write the creative in {language_name}, written natively rather than translated.\n"
        f"Change exactly this primary experiment dimension: {changed_dimension}.\n"
        "Return strict JSON following the exact schema from the system instruction. Do not use locale labels "
        "or placeholders in the copy.\n"
        "Every field outside the selected dimension must exactly equal the frozen source.\n"
        f"Frozen source JSON: {json.dumps(source, sort_keys=True, ensure_ascii=False)}\n"
    )
    if learnings:
        prompt += "Matching valid learnings (hypotheses only):\n"
        prompt += "\n".join(f"- {item[:300]}" for item in learnings[:5]) + "\n"
    return prompt


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
    learnings: tuple[str, ...] = (),
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
            learnings=learnings,
        ),
        temperature=0.7,
        max_tokens=600,
    )
    payload = parse_claude_json(resp.text)
    validate_meta(
        {**payload, "audience_profile_key": audience_profile_key},
        allowed_audiences=set(AUDIENCE_PROFILES),
    )
    primary_text = payload["primary_text"]
    headline = payload["headline"]
    description = payload["description"]
    cta_label = payload["cta_label"]
    budget = int(payload["suggested_daily_budget_eur"])

    return MetaAdCreative(
        primary_text=primary_text,
        headline=headline,
        description=description,
        cta_label=cta_label,
        suggested_daily_budget_eur=budget,
        audience_profile_key=audience_profile_key,
        cost_cents=_cost_cents(resp),
    )
