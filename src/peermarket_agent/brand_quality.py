"""Brand-quality gate — Claude scores a draft against brand_voice.md.

The gate runs after generation, before persistence. Drafts scoring < 80
are rejected internally and never reach the drafts table — keeps the
approval queue free of obvious off-brand content.
"""

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.claude import ClaudeClient

BRAND_SCORE_THRESHOLD = 80


def _system_prompt(brand_voice_md: str) -> str:
    return (
        "You evaluate marketing copy against PeerMarket's brand voice.\n\n"
        "Brand voice rules (READ FIRST):\n"
        "----\n"
        f"{brand_voice_md}\n"
        "----\n\n"
        "Your job: score the given copy 0-100 for brand-voice alignment.\n"
        "Output a JSON object with exactly two fields:\n"
        "{\n"
        '  "score": <integer 0-100>,\n'
        '  "notes": "<one short sentence on what works or fails>"\n'
        "}\n\n"
        "Score 0 = totally off brand, 100 = perfect match. Be strict but fair.\n"
    )


async def score_draft(
    *,
    claude: ClaudeClient,
    brand_voice_md: str,
    copy: str,
) -> tuple[int, str]:
    """Return (score, notes). Score is clamped to [0, 100]."""
    resp = await claude.complete(
        system=_system_prompt(brand_voice_md),
        user=f"Copy to evaluate:\n----\n{copy}\n----\n",
        temperature=0.0,  # deterministic scoring
        max_tokens=200,
    )
    payload = parse_claude_json(resp.text)
    score = int(payload["score"])
    score = max(0, min(100, score))
    notes = str(payload.get("notes", ""))
    return score, notes
