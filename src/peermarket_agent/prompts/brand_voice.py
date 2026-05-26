"""Brand voice — file-on-disk source of truth, DB sync on every boot."""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

BRAND_VOICE_FILE = Path(__file__).parent.parent.parent.parent / "config" / "brand_voice.md"


def load_brand_voice() -> str:
    return BRAND_VOICE_FILE.read_text(encoding="utf-8")


async def sync_to_db(engine: AsyncEngine) -> None:
    """Mirror config/brand_voice.md into brand_voice.voice_rules_md.

    Idempotent — always overwrites. Source of truth is the file.
    """
    md = load_brand_voice()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO brand_voice (id, voice_rules_md, updated_at) "
                "VALUES (1, :md, NOW()) "
                "ON CONFLICT (id) DO UPDATE "
                "SET voice_rules_md = EXCLUDED.voice_rules_md, "
                "    updated_at = EXCLUDED.updated_at"
            ),
            {"md": md},
        )
