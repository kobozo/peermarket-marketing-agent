"""Secure Slack video download, inspection, validation, and normalization."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from peermarket_agent.slack_bridge.video_events import VideoUpload

_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_SAFE_SLACK_FILE_ID = re.compile(r"[A-Za-z0-9]+$")
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class MediaLimits:
    max_bytes: int
    max_clips: int
    max_duration_seconds: int


@dataclass(frozen=True)
class MediaMetadata:
    path: Path
    mime_type: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    has_audio: bool


def _extension_for_upload(file_info: VideoUpload) -> str:
    extension = Path(file_info.filename).suffix.lower()
    expected_mime = _VIDEO_TYPES.get(extension)
    if expected_mime is None or file_info.mimetype.lower() != expected_mime:
        raise ValueError("MIME type does not match the uploaded filename extension")
    return extension


def _private_download_url(file_details: object) -> str:
    get = getattr(file_details, "get", None)
    if not callable(get):
        raise ValueError("Slack did not return file metadata")
    file_data = get("file")
    if not isinstance(file_data, dict):
        raise ValueError("Slack did not return file metadata")
    url = file_data.get("url_private_download")
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme != "https" or parsed.hostname != "files.slack.com":
        raise ValueError("Slack returned an invalid private download URL")
    return url


async def download_slack_file(
    client, file_info: VideoUpload, destination: Path, max_bytes: int
) -> Path:
    """Download one Slack file with bot authentication and a strict byte cap."""
    if max_bytes <= 0:
        raise ValueError("Maximum download size must be positive")
    extension = _extension_for_upload(file_info)
    if file_info.size_bytes > max_bytes:
        raise ValueError("Uploaded file exceeds the maximum allowed size")
    if not _SAFE_SLACK_FILE_ID.fullmatch(file_info.file_id):
        raise ValueError("Slack file ID contains unsafe characters")

    file_details = await client.files_info(file=file_info.file_id)
    url = _private_download_url(file_details)
    token = getattr(client, "token", None)
    if not isinstance(token, str) or not token:
        raise ValueError("Slack client has no bot token")

    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    saved_path = destination / f"{file_info.file_id}{extension}"
    partial_path = destination / f".{file_info.file_id}{extension}.part"
    if await asyncio.to_thread(saved_path.exists) or await asyncio.to_thread(partial_path.exists):
        raise FileExistsError(f"Video output already exists: {saved_path}")

    received = 0
    output = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as response:
                response.raise_for_status()
                output = await asyncio.to_thread(partial_path.open, "xb")
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    received += len(chunk)
                    if received > max_bytes:
                        raise ValueError("Downloaded file exceeds the maximum allowed size")
                    await asyncio.to_thread(output.write, chunk)
        await asyncio.to_thread(output.close)
        output = None
        await asyncio.to_thread(partial_path.replace, saved_path)
    except BaseException:
        if output is not None:
            await asyncio.to_thread(output.close)
        await asyncio.to_thread(partial_path.unlink, missing_ok=True)
        raise
    return saved_path


async def inspect_video(path: Path) -> MediaMetadata:
    """Read deterministic media facts from ffprobe's JSON output."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace').strip()}")
    try:
        payload = json.loads(stdout)
        streams = payload["streams"]
        video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration_seconds = float(payload["format"]["duration"])
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe returned incomplete or invalid video metadata") from exc

    return MediaMetadata(
        path=path,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        size_bytes=(await asyncio.to_thread(path.stat)).st_size,
        duration_seconds=duration_seconds,
        width=width,
        height=height,
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


def validate_media(metadata: MediaMetadata, limits: MediaLimits) -> list[str]:
    """Return user-facing warnings for media outside TikTok intake limits."""
    warnings: list[str] = []
    if metadata.size_bytes > limits.max_bytes:
        warnings.append(f"Video exceeds the {limits.max_bytes}-byte upload limit.")
    if metadata.duration_seconds <= 0 or metadata.duration_seconds > limits.max_duration_seconds:
        warnings.append(
            f"Video duration must be between 0 and {limits.max_duration_seconds} seconds."
        )
    if metadata.width <= metadata.height:
        return warnings
    warnings.append("Video must be vertical (portrait), not landscape.")
    return warnings


async def normalize_video(source: Path, destination: Path) -> MediaMetadata:
    """Produce a TikTok-ready H.264/AAC, 1080x1920, 30fps MP4."""
    source_path = await asyncio.to_thread(source.resolve)
    destination_path = await asyncio.to_thread(destination.resolve)
    if source_path == destination_path:
        raise ValueError("Video source and destination must be different paths")
    if await asyncio.to_thread(destination.exists):
        try:
            if await asyncio.to_thread(destination.samefile, source):
                raise ValueError("Video source and destination must be different paths")
        except FileNotFoundError:
            pass
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg normalization failed: {stderr.decode(errors='replace').strip()}"
        )
    return await inspect_video(destination)
