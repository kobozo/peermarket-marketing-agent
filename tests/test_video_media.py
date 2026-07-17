"""Tests for bounded Slack video intake and deterministic media processing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from peermarket_agent.slack_bridge.video_events import VideoUpload
from peermarket_agent.video_media import (
    MediaLimits,
    MediaMetadata,
    download_slack_file,
    inspect_video,
    normalize_video,
    validate_media,
)


def _upload(**overrides) -> VideoUpload:
    values = {
        "file_id": "F0123SAFE",
        "thread_ts": "1710000000.123456",
        "channel_id": "C123",
        "user_id": "U123",
        "filename": "../../untrusted.mp4",
        "mimetype": "video/mp4",
        "size_bytes": 3,
    }
    values.update(overrides)
    return VideoUpload(**values)


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.content = _FakeContent(chunks)
        self.status_checked = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        self.status_checked = True


class _FakeSession:
    def __init__(self, response: _FakeResponse, calls: list[tuple[str, dict]]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url: str, **kwargs):
        self._calls.append((url, kwargs))
        return self._response


class _FakeClient:
    token = "xoxb-private-token"

    async def files_info(self, *, file: str):
        assert file == "F0123SAFE"
        return _ResponseLike({"file": {"url_private_download": "https://files.slack.com/private"}})


class _ResponseLike:
    """Matches Slack's AsyncSlackResponse mapping-style interface."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


async def test_download_uses_bot_token_and_slack_id_not_untrusted_filename(tmp_path, monkeypatch):
    from peermarket_agent import video_media

    response = _FakeResponse([b"abc"])
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        video_media.aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeSession(response, calls),
    )

    saved = await download_slack_file(_FakeClient(), _upload(), tmp_path, max_bytes=10)

    assert saved == tmp_path / "F0123SAFE.mp4"
    assert saved.read_bytes() == b"abc"
    assert response.status_checked is True
    assert calls == [
        (
            "https://files.slack.com/private",
            {"headers": {"Authorization": "Bearer xoxb-private-token"}},
        )
    ]


@pytest.mark.parametrize(
    ("filename", "mimetype"),
    [
        ("clip.mp4", "video/quicktime"),
        ("clip.exe", "video/mp4"),
    ],
)
async def test_download_rejects_mime_extension_mismatch_before_network(
    tmp_path, filename, mimetype
):
    with pytest.raises(ValueError, match="MIME type"):
        await download_slack_file(
            _FakeClient(), _upload(filename=filename, mimetype=mimetype), tmp_path, max_bytes=10
        )


async def test_download_rejects_declared_or_streamed_size_over_limit(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="maximum"):
        await download_slack_file(_FakeClient(), _upload(size_bytes=11), tmp_path, max_bytes=10)

    from peermarket_agent import video_media

    monkeypatch.setattr(
        video_media.aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeSession(_FakeResponse([b"12345", b"67890", b"x"]), []),
    )
    with pytest.raises(ValueError, match="maximum"):
        await download_slack_file(_FakeClient(), _upload(size_bytes=0), tmp_path, max_bytes=10)
    assert list(tmp_path.iterdir()) == []


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


async def test_inspect_video_parses_ffprobe_json(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    payload = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920},
            {"codec_type": "audio"},
        ],
    }
    commands: list[tuple] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append((args, kwargs))
        return _FakeProcess(json.dumps(payload).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    metadata = await inspect_video(path)

    assert metadata == MediaMetadata(
        path=path,
        mime_type="video/mp4",
        size_bytes=5,
        duration_seconds=12.5,
        width=1080,
        height=1920,
        has_audio=True,
    )
    assert commands[0][0][:4] == ("ffprobe", "-v", "error", "-show_entries")
    assert "json" in commands[0][0]


def test_validate_media_warns_for_landscape_and_duration_outside_limits(tmp_path):
    metadata = MediaMetadata(
        path=tmp_path / "clip.mp4",
        mime_type="video/mp4",
        size_bytes=100,
        duration_seconds=61,
        width=1920,
        height=1080,
        has_audio=True,
    )

    warnings = validate_media(
        metadata, MediaLimits(max_bytes=200, max_clips=8, max_duration_seconds=60)
    )

    assert len(warnings) == 2
    assert any("vertical" in warning.lower() for warning in warnings)
    assert any("60" in warning for warning in warnings)


async def test_normalize_video_rejects_hard_link_destination_before_ffmpeg(tmp_path, monkeypatch):
    source = tmp_path / "source.webm"
    destination = tmp_path / "normalized.mp4"
    source.write_bytes(b"source")
    destination.hardlink_to(source)
    commands: list[tuple] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append((args, kwargs))
        return _FakeProcess(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(ValueError, match="source and destination"):
        await normalize_video(source, destination)

    assert commands == []


async def test_normalize_video_constructs_tiktok_ffmpeg_command(tmp_path, monkeypatch):
    from peermarket_agent import video_media

    source = tmp_path / "source.webm"
    destination = tmp_path / "normalized.mp4"
    source.write_bytes(b"source")
    commands: list[tuple] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append((args, kwargs))
        return _FakeProcess(b"")

    expected = MediaMetadata(destination, "video/mp4", 10, 12.0, 1080, 1920, True)

    async def fake_inspect(path: Path) -> MediaMetadata:
        assert path == destination
        return expected

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(video_media, "inspect_video", fake_inspect)

    source_alias = tmp_path / "must-not-exist" / ".." / source.name
    with pytest.raises(ValueError, match="source and destination"):
        await normalize_video(source, source_alias)
    assert commands == []
    assert not (tmp_path / "must-not-exist").exists()

    hardlink_alias = tmp_path / "source-hardlink.webm"
    hardlink_alias.hardlink_to(source)
    with pytest.raises(ValueError, match="source and destination"):
        await normalize_video(source, hardlink_alias)
    assert commands == []

    assert await normalize_video(source, destination) == expected

    args, kwargs = commands[0]
    assert args == (
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(destination),
    )
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
