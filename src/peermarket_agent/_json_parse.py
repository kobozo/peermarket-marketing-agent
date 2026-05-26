"""Robust JSON parsing for Claude responses.

Claude (recent Sonnet/Opus) sometimes wraps JSON in markdown fences like
```json ... ``` even when the prompt forbids it. This module strips
fences before json.loads.
"""

import json
import re

_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(.*?)\n```\s*$",
    re.DOTALL,
)


def parse_claude_json(text: str) -> dict:
    """Parse JSON from a Claude response, stripping optional markdown fences.

    Raises ValueError (not JSONDecodeError) on parse failure, with the
    raw text truncated to 200 chars for debugging.
    """
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned not valid JSON: {text[:200]!r}") from e
