"""Regex parser for ✅/❌ <draft-id> acknowledgements in Slack DMs.

Pure function — no DB, no Slack client. Accepts text, returns
(action, draft_id) or None if no recognizable pattern.
"""

from __future__ import annotations

import re
from typing import Literal

AckAction = Literal["approve", "reject"]

# Match the emoji glyph OR the colon-code, optional whitespace, then digits.
# Case-insensitive on the colon-code (`:WHITE_CHECK_MARK:` is rare but valid).
_PATTERN = re.compile(
    r"(?P<emoji>✅|❌|:white_check_mark:|:x:)\s*(?P<id>\d+)",
    re.IGNORECASE,
)


def parse_ack(text: str) -> tuple[AckAction, int] | None:
    """Return ('approve', id) or ('reject', id) if text contains a valid ack pattern."""
    m = _PATTERN.search(text or "")
    if not m:
        return None
    emoji = m.group("emoji").lower()
    draft_id = int(m.group("id"))
    if emoji in ("✅", ":white_check_mark:"):
        return ("approve", draft_id)
    if emoji in ("❌", ":x:"):
        return ("reject", draft_id)
    return None
