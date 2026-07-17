"""Async Claude client wrapper.

Standardizes how the agent talks to Claude — single retry-safe interface,
explicit input/output token reporting, model defaulting to Sonnet 4.6.
"""

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic

DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class ClaudeResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str


class ClaudeClient:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> ClaudeResponse:
        resp = await self._client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        # Concatenate all text blocks; ignore non-text content (tool use, etc.)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return ClaudeResponse(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=resp.model,
            stop_reason=resp.stop_reason,
        )

    async def complete_with_images(
        self,
        *,
        system: str,
        user: str,
        images: list[Path],
        model: str = DEFAULT_MODEL,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> ClaudeResponse:
        """Complete a prompt with JPEG image blocks in the supplied order."""
        content = []
        for image in images:
            if image.suffix.lower() not in {".jpg", ".jpeg"}:
                raise ValueError(f"Claude vision inputs must be JPEG files: {image}")
            encoded = base64.b64encode(await asyncio.to_thread(image.read_bytes)).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encoded,
                    },
                }
            )
        content.append({"type": "text", "text": user})
        resp = await self._client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": content}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return ClaudeResponse(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=resp.model,
            stop_reason=resp.stop_reason,
        )
