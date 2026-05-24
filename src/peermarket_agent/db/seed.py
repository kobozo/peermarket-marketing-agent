"""Idempotent seed data. Applied on every service start, after migrations."""
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_SEED_FILE = Path(__file__).parent.parent.parent.parent / "config" / "action_types.yaml"

_DEFAULT_BRAND_VOICE = """\
# PeerMarket brand voice rules (seed)

- Belgian roots. Dry, slightly understated humor. Never American hype.
- Languages: NL and FR are primary. EN allowed for SEO landing pages.
- No em-dashes (—). Use commas or short sentences instead.
- Trust + verified identity are the wedge. Lean into them, do not soften.
- Never make medical, legal, or financial claims about anything.
- Never use a competitor brand name in paid ad copy. Organic comparison OK.

## Visual truthfulness (non-negotiable, see spec §16)

- Never generate synthetic depictions of products, people, or transactions.
- Permitted visuals: real peermarket.eu screenshots, real photos with consent,
  abstract branded graphics (typography/color/icons), Pillow/Recraft frame assets,
  free-license stylized stock.
"""


async def seed(engine: AsyncEngine) -> None:
    data = yaml.safe_load(_SEED_FILE.read_text())
    async with engine.begin() as conn:
        for at in data["action_types"]:
            await conn.execute(
                text(
                    "INSERT INTO action_types (name, risk_tier, default_autonomy) "
                    "VALUES (:n, :r, :d) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "risk_tier = EXCLUDED.risk_tier, "
                    "default_autonomy = EXCLUDED.default_autonomy"
                ),
                {"n": at["name"], "r": at["risk_tier"], "d": at["default_autonomy"]},
            )
        await conn.execute(text(
            "INSERT INTO trust_scores (action_type_id, current_mode) "
            "SELECT id, default_autonomy FROM action_types "
            "ON CONFLICT (action_type_id) DO NOTHING"
        ))
        await conn.execute(
            text(
                "INSERT INTO brand_voice (id, voice_rules_md) VALUES (1, :v) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"v": _DEFAULT_BRAND_VOICE},
        )
