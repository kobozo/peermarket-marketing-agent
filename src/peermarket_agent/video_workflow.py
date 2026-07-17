"""Orchestrate secure Slack video intake, review, and clip combination."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from sqlalchemy import text

from peermarket_agent.drafts import (
    VideoAsset,
    claim_video_asset,
    persist_video_asset,
)
from peermarket_agent.slack_bridge.video_events import VideoUpload, find_thread_draft
from peermarket_agent.video_media import (
    MediaLimits,
    MediaMetadata,
    download_slack_file,
    inspect_video,
    normalize_video,
    validate_media,
)
from peermarket_agent.video_review import extract_keyframes, review_video

_draft_locks: dict[tuple[int, int], asyncio.Lock] = {}


@dataclass(frozen=True)
class WorkflowResult:
    status: Literal["accepted", "rejected", "reviewed", "combined", "failed"]
    message: str
    output_path: Path | None


def _draft_lock(draft_id: int) -> asyncio.Lock:
    loop_key = (id(asyncio.get_running_loop()), draft_id)
    return _draft_locks.setdefault(loop_key, asyncio.Lock())


async def _acquire_draft_file_lock(root: Path):
    """Acquire the cross-process lock for all deterministic draft media paths."""
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    handle = await asyncio.to_thread((root / ".workflow.lock").open, "a+")
    try:
        await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        await asyncio.to_thread(handle.close)
        raise
    return handle


async def _release_draft_file_lock(handle) -> None:
    await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
    await asyncio.to_thread(handle.close)


@asynccontextmanager
async def _draft_file_lock(root: Path):
    """Serialize complete draft workflows across worker processes."""
    handle = await _acquire_draft_file_lock(root)
    try:
        yield
    finally:
        await _release_draft_file_lock(handle)


def _limits(settings) -> MediaLimits:
    return MediaLimits(
        max_bytes=settings.video_max_file_bytes,
        max_clips=settings.video_max_clips,
        max_duration_seconds=settings.video_max_duration_seconds,
    )


def _source_path(root: Path, upload: VideoUpload) -> Path:
    return root / "sources" / f"{upload.file_id}{Path(upload.filename).suffix.lower()}"


def _normalized_path(root: Path, file_id: str) -> Path:
    return root / "exports" / f"{file_id}-normalized.mp4"


def _result_from_asset(asset: VideoAsset) -> WorkflowResult:
    result = asset.review.get("workflow_result", {}) if isinstance(asset.review, dict) else {}
    status = result.get("status", asset.status)
    if status not in {"accepted", "rejected", "reviewed", "combined", "failed"}:
        status = "accepted"
    output = result.get("output_path")
    return WorkflowResult(
        status,
        result.get("message", "This upload was already received."),
        Path(output) if output else None,
    )


def _review_payload(result: WorkflowResult, review: dict | None = None) -> dict:
    payload = dict(review or {})
    payload["workflow_result"] = {
        "status": result.status,
        "message": result.message,
        "output_path": str(result.output_path) if result.output_path else None,
    }
    return payload


async def get_draft_copy(engine, draft_id: int) -> str:
    """Retrieve the approved copy only after the thread lookup has authorized it."""
    async with engine.connect() as conn:
        copy = await conn.scalar(
            text("SELECT copy FROM drafts WHERE id = :draft_id"), {"draft_id": draft_id}
        )
    if not isinstance(copy, str):
        raise ValueError("Draft copy is unavailable")
    return copy


async def update_video_asset(engine, asset: VideoAsset) -> None:
    """Update processing fields for a previously persisted video asset."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE video_assets SET path = :path, mime_type = :mime_type, "
                "size_bytes = :size_bytes, duration_seconds = :duration_seconds, width = :width, "
                "height = :height, status = :status, review = CAST(:review AS JSONB) "
                "WHERE draft_id = :draft_id AND slack_file_id = :slack_file_id"
            ),
            {
                "draft_id": asset.draft_id,
                "slack_file_id": asset.slack_file_id,
                "path": asset.path,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "duration_seconds": asset.duration_seconds,
                "width": asset.width,
                "height": asset.height,
                "status": asset.status,
                "review": json.dumps(asset.review),
            },
        )


async def get_source_assets(engine, draft_id: int) -> list[VideoAsset]:
    """Return persisted Slack sources in their original upload order."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT draft_id, slack_file_id, thread_ts, path, role, mime_type, size_bytes, "
                    "duration_seconds, width, height, status, review, message_ts FROM video_assets "
                    "WHERE draft_id = :draft_id AND role = 'source' "
                    "AND status NOT IN ('rejected', 'failed') "
                    "ORDER BY NULLIF(message_ts, ''), id"
                ),
                {"draft_id": draft_id},
            )
        ).fetchall()
    return [
        VideoAsset(
            draft_id=row[0],
            slack_file_id=row[1],
            thread_ts=row[2],
            path=row[3],
            role=row[4],
            mime_type=row[5],
            size_bytes=row[6],
            duration_seconds=row[7],
            width=row[8],
            height=row[9],
            status=row[10],
            review=row[11],
            message_ts=row[12],
        )
        for row in rows
    ]


async def combine_video_clips(paths: list[Path], output: Path) -> MediaMetadata:
    """Concatenate normalized MP4 clips in the supplied order using ffmpeg."""
    if len(paths) < 2:
        raise ValueError("At least two clips are required for combination")
    if any(not path.is_file() for path in paths):
        raise ValueError("All clips must exist before combination")
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    manifest = output.with_suffix(".concat.txt")
    try:
        await asyncio.to_thread(
            manifest.write_text,
            "".join(f"file '{path.as_posix().replace("'", r"'\\''")}'\n" for path in paths),
            "utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"ffmpeg combination failed: {stderr.decode(errors='replace').strip()}"
            )
        return await inspect_video(output)
    except BaseException:
        await asyncio.to_thread(output.unlink, missing_ok=True)
        raise
    finally:
        await asyncio.to_thread(manifest.unlink, missing_ok=True)


async def _reply(slack_client, upload: VideoUpload, text_message: str) -> None:
    with contextlib.suppress(Exception):
        await slack_client.chat_postMessage(
            channel=upload.channel_id,
            thread_ts=upload.thread_ts,
            text=text_message,
        )


def _remove_work(path: Path) -> None:
    """Remove only the short-lived per-upload work directory."""
    shutil.rmtree(path, ignore_errors=True)


async def _store_result(
    engine, asset: VideoAsset, result: WorkflowResult, review: dict | None = None
) -> None:
    await update_video_asset(
        engine,
        replace(asset, status=result.status, review=_review_payload(result, review)),
    )


async def process_video_upload(
    engine, slack_client, claude, upload: VideoUpload, settings
) -> WorkflowResult:
    """Handle all workflow-boundary failures with a thread-visible result."""
    try:
        return await _process_video_upload(engine, slack_client, claude, upload, settings)
    except Exception as exc:
        result = WorkflowResult("failed", f"Video could not be processed: {exc}", None)
        await _reply(slack_client, upload, result.message)
        return result


async def _process_video_upload(
    engine, slack_client, claude, upload: VideoUpload, settings
) -> WorkflowResult:
    """Process one thread upload without trusting the event router for authorization."""
    draft_id = await find_thread_draft(engine, upload.channel_id, upload.thread_ts)
    if draft_id is None:
        result = WorkflowResult(
            "rejected", "This upload is not in an active TikTok draft thread.", None
        )
        await _reply(slack_client, upload, result.message)
        return result
    if not settings.slack_founder_user_id or upload.user_id != settings.slack_founder_user_id:
        result = WorkflowResult(
            "rejected", "Only the authorized founder can upload recording clips.", None
        )
        await _reply(slack_client, upload, result.message)
        return result

    async with _draft_lock(draft_id):
        root = Path(settings.video_media_root) / f"draft-{draft_id}"
        file_lock = await _acquire_draft_file_lock(root)
        source_path = _source_path(root, upload)
        asset = VideoAsset(
            draft_id=draft_id,
            slack_file_id=upload.file_id,
            thread_ts=upload.thread_ts,
            path=str(source_path),
            role="source",
            mime_type=upload.mimetype,
            size_bytes=upload.size_bytes,
            duration_seconds=None,
            width=None,
            height=None,
            status="accepted",
            review={},
            message_ts=upload.message_ts or upload.thread_ts,
        )
        try:
            asset, claimed = await claim_video_asset(engine, asset)
        except BaseException:
            await _release_draft_file_lock(file_lock)
            raise
        if not claimed:
            await _release_draft_file_lock(file_lock)
            return _result_from_asset(asset)
        work = root / "work" / upload.file_id
        normalized = _normalized_path(root, upload.file_id)
        try:
            sources = await get_source_assets(engine, draft_id)
            if len(sources) > settings.video_max_clips:
                result = WorkflowResult(
                    "rejected",
                    f"A draft can include at most {settings.video_max_clips} clips.",
                    None,
                )
                await _store_result(engine, asset, result)
                await _reply(slack_client, upload, result.message)
                return result

            downloaded = await download_slack_file(
                slack_client, upload, source_path.parent, settings.video_max_file_bytes
            )
            metadata = await inspect_video(downloaded)
            asset = replace(
                asset,
                path=str(downloaded),
                mime_type=metadata.mime_type,
                size_bytes=metadata.size_bytes,
                duration_seconds=metadata.duration_seconds,
                width=metadata.width,
                height=metadata.height,
            )
            warnings = validate_media(metadata, _limits(settings))
            if warnings:
                result = WorkflowResult("rejected", " ".join(warnings), None)
                await _store_result(engine, asset, result)
                await _reply(slack_client, upload, result.message)
                return result

            normalized_metadata = await normalize_video(downloaded, normalized)
            sources = await get_source_assets(engine, draft_id)
            normalized_sources = [
                _normalized_path(root, source.slack_file_id) for source in sources
            ]
            for source, path in zip(sources, normalized_sources, strict=True):
                if not path.exists():
                    if not await asyncio.to_thread(Path(source.path).is_file):
                        result = WorkflowResult(
                            "failed",
                            "Another accepted recording clip is still being stored. Please retry shortly.",
                            None,
                        )
                        await _store_result(engine, asset, result)
                        await _reply(slack_client, upload, result.message)
                        return result
                    await normalize_video(Path(source.path), path)

            if len(normalized_sources) == 1:
                output = normalized
                output_metadata = normalized_metadata
                status: Literal["reviewed", "combined"] = "reviewed"
            else:
                output = root / "combined" / f"{upload.file_id}-combined.mp4"
                output_metadata = await combine_video_clips(normalized_sources, output)
                status = "combined"

            frames = await extract_keyframes(output, work / "frames")
            review = await review_video(
                claude, await get_draft_copy(engine, draft_id), output_metadata, frames
            )
            result = WorkflowResult(status, review.summary, output)
            await _store_result(
                engine,
                asset,
                result,
                {
                    "decision": review.decision,
                    "strengths": review.strengths,
                    "changes": review.changes,
                    "script_alignment": review.script_alignment,
                    "edit_points": review.edit_points,
                    "summary": review.summary,
                },
            )
            if status == "combined":
                combined_asset = VideoAsset(
                    draft_id=draft_id,
                    slack_file_id=f"combined:{upload.file_id}",
                    thread_ts=upload.thread_ts,
                    path=str(output),
                    role="combined",
                    mime_type=output_metadata.mime_type,
                    size_bytes=output_metadata.size_bytes,
                    duration_seconds=output_metadata.duration_seconds,
                    width=output_metadata.width,
                    height=output_metadata.height,
                    status=status,
                    review=_review_payload(result),
                    message_ts=upload.message_ts or upload.thread_ts,
                )
                await persist_video_asset(engine, combined_asset)
            await _reply(slack_client, upload, review.summary)
            return result
        except Exception as exc:
            await asyncio.to_thread(normalized.unlink, missing_ok=True)
            result = WorkflowResult("failed", f"Video could not be processed: {exc}", None)
            await _store_result(engine, asset, result)
            await _reply(slack_client, upload, result.message)
            return result
        finally:
            _remove_work(work)
            await _release_draft_file_lock(file_lock)
