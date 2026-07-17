"""Video frame review tests — no real Claude or ffmpeg calls."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.video_media import MediaMetadata
from peermarket_agent.video_review import VideoReview, extract_keyframes, review_video


def _metadata(path: Path) -> MediaMetadata:
    return MediaMetadata(
        path=path,
        mime_type="video/mp4",
        size_bytes=100,
        duration_seconds=12.0,
        width=1080,
        height=1920,
        has_audio=True,
    )


async def test_extract_keyframes_uses_ffmpeg_and_returns_ordered_jpegs(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    frames_dir = tmp_path / "frames"
    commands: list[tuple] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            for number in (1, 2):
                (frames_dir / f"frame-{number:02d}.jpg").write_bytes(b"jpeg")
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    frames = await extract_keyframes(video, frames_dir, count=2)

    assert frames == [frames_dir / "frame-01.jpg", frames_dir / "frame-02.jpg"]
    assert commands[0][0] == (
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        "select='eq(pict_type,I)'",
        "-vsync",
        "0",
        "-frames:v",
        "2",
        str(frames_dir / "frame-%02d.jpg"),
    )


async def test_review_video_parses_valid_json_into_video_review(tmp_path):
    frame = tmp_path / "frame-01.jpg"
    frame.write_bytes(b"jpeg")
    claude = AsyncMock()
    claude.complete_with_images = AsyncMock(
        return_value=ClaudeResponse(
            text=(
                '{"decision":"needs_changes","strengths":["clear hook"],'
                '"changes":["brighten the frame"],'
                '"script_alignment":["opening matches"],'
                '"edit_points":["trim first second"],'
                '"summary":"Strong take; improve lighting."}'
            ),
            input_tokens=20,
            output_tokens=30,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )

    result = await review_video(claude, "Say hello", _metadata(tmp_path / "clip.mp4"), [frame])

    assert result == VideoReview(
        decision="needs_changes",
        strengths=["clear hook"],
        changes=["brighten the frame"],
        script_alignment=["opening matches"],
        edit_points=["trim first second"],
        summary="Strong take; improve lighting.",
    )
    assert claude.complete_with_images.await_args.kwargs["images"] == [frame]
    assert claude.complete_with_images.await_args.kwargs["user"].endswith("Say hello")


@pytest.mark.parametrize(
    "response_text, message",
    [
        ("not json", "not valid JSON"),
        ('{"decision":"pass"}', "must contain exactly"),
        (
            '{"decision":"maybe","strengths":[],"changes":[],"script_alignment":[],'
            '"edit_points":[],"summary":"x"}',
            "decision",
        ),
        (
            '{"decision":[],"strengths":[],"changes":[],"script_alignment":[],'
            '"edit_points":[],"summary":"x"}',
            "decision",
        ),
        (
            '{"decision":"pass","strengths":"good","changes":[],"script_alignment":[],'
            '"edit_points":[],"summary":"x"}',
            "strengths",
        ),
    ],
)
async def test_review_video_rejects_malformed_json(response_text, message, tmp_path):
    claude = AsyncMock()
    claude.complete_with_images = AsyncMock(
        return_value=ClaudeResponse(response_text, 1, 1, "claude-sonnet-4-6", "end_turn")
    )

    with pytest.raises(ValueError, match=message):
        await review_video(claude, "Say hello", _metadata(tmp_path / "clip.mp4"), [])


async def test_review_video_propagates_claude_failure_for_retry_handling(tmp_path):
    claude = AsyncMock()
    claude.complete_with_images = AsyncMock(side_effect=RuntimeError("Claude unavailable"))

    with pytest.raises(RuntimeError, match="Claude unavailable"):
        await review_video(claude, "Say hello", _metadata(tmp_path / "clip.mp4"), [])
