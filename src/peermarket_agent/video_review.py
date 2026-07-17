"""Claude vision review of sampled video keyframes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.video_media import MediaMetadata

_REVIEW_FIELDS = {
    "decision",
    "strengths",
    "changes",
    "script_alignment",
    "edit_points",
    "summary",
}
_DECISIONS = {"pass", "needs_changes", "reject"}


@dataclass(frozen=True)
class VideoReview:
    decision: Literal["pass", "needs_changes", "reject"]
    strengths: list[str]
    changes: list[str]
    script_alignment: list[str]
    edit_points: list[str]
    summary: str


async def extract_keyframes(video: Path, output_dir: Path, count: int = 6) -> list[Path]:
    """Extract up to ``count`` JPEG I-frames from a video in presentation order."""
    if count <= 0:
        raise ValueError("Keyframe count must be positive")
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    pattern = output_dir / "frame-%02d.jpg"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        "select='eq(pict_type,I)'",
        "-vsync",
        "0",
        "-frames:v",
        str(count),
        str(pattern),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg keyframe extraction failed: {stderr.decode(errors='replace').strip()}"
        )
    frames = sorted(await asyncio.to_thread(lambda: list(output_dir.glob("frame-*.jpg"))))
    if not frames:
        raise ValueError("ffmpeg extracted no JPEG keyframes")
    return frames


def _system_prompt() -> str:
    return """You review recorded TikTok footage against an approved script.

Only describe what is visibly supported by the supplied frames and the technical metadata.
Do not invent spoken words, events, timing, or details that are not visible. Be concise and useful
to the founder. Return JSON only, with exactly this schema:
{
  "decision": "pass" | "needs_changes" | "reject",
  "strengths": ["string"],
  "changes": ["string"],
  "script_alignment": ["string"],
  "edit_points": ["string"],
  "summary": "string"
}
"""


def _parse_review(text: str) -> VideoReview:
    payload = parse_claude_json(text)
    if not isinstance(payload, dict):
        raise ValueError("Claude video review must be a JSON object")
    if set(payload) != _REVIEW_FIELDS:
        raise ValueError("Claude video review must contain exactly the required fields")
    decision = payload["decision"]
    if not isinstance(decision, str) or decision not in _DECISIONS:
        raise ValueError("Claude video review decision must be pass, needs_changes, or reject")
    list_fields = ("strengths", "changes", "script_alignment", "edit_points")
    for field in list_fields:
        value = payload[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Claude video review {field} must be a list of strings")
    summary = payload["summary"]
    if not isinstance(summary, str):
        raise ValueError("Claude video review summary must be a string")
    return VideoReview(
        decision=decision,
        strengths=payload["strengths"],
        changes=payload["changes"],
        script_alignment=payload["script_alignment"],
        edit_points=payload["edit_points"],
        summary=summary,
    )


async def review_video(
    claude: ClaudeClient,
    script: str,
    metadata: MediaMetadata,
    frames: list[Path],
) -> VideoReview:
    """Review keyframes against the approved script without hiding Claude failures."""
    user = (
        "Technical metadata:\n"
        f"- MIME type: {metadata.mime_type}\n"
        f"- Duration: {metadata.duration_seconds:.2f} seconds\n"
        f"- Dimensions: {metadata.width}x{metadata.height}\n"
        f"- Audio present: {metadata.has_audio}\n\n"
        "Approved script:\n"
        f"{script}"
    )
    response = await claude.complete_with_images(
        system=_system_prompt(),
        user=user,
        images=frames,
        temperature=0.0,
        max_tokens=700,
    )
    return _parse_review(response.text)
