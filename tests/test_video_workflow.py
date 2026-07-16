"""End-to-end orchestration tests for Slack recording uploads."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.drafts import VideoAsset
from peermarket_agent.slack_bridge.video_events import VideoUpload
from peermarket_agent.video_media import MediaMetadata
from peermarket_agent.video_review import VideoReview
from peermarket_agent.video_workflow import (
    WorkflowResult,
    _draft_file_lock,
    process_video_upload,
)


def _upload(**overrides) -> VideoUpload:
    values = {
        "file_id": "F001",
        "thread_ts": "1710000000.123456",
        "channel_id": "C123",
        "user_id": "UFOUNDER",
        "filename": "take.mp4",
        "mimetype": "video/mp4",
        "size_bytes": 100,
    }
    values.update(overrides)
    return VideoUpload(**values)


def _settings(tmp_path: Path, **overrides):
    values = {
        "slack_founder_user_id": "UFOUNDER",
        "video_media_root": tmp_path / "media",
        "video_max_file_bytes": 200,
        "video_max_clips": 2,
        "video_max_duration_seconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metadata(path: Path) -> MediaMetadata:
    return MediaMetadata(path, "video/mp4", 100, 12.0, 1080, 1920, True)


@pytest.fixture
def boundaries(monkeypatch, tmp_path):
    from peermarket_agent import video_workflow

    assets = {}
    monkeypatch.setattr(video_workflow, "find_thread_draft", AsyncMock(return_value=42))
    monkeypatch.setattr(video_workflow, "get_draft_copy", AsyncMock(return_value="Approved script"))

    async def claim_asset(_engine, asset):
        existing = assets.get((asset.draft_id, asset.slack_file_id))
        if existing is not None:
            return existing, False
        assets[(asset.draft_id, asset.slack_file_id)] = asset
        return asset, True

    async def persist_asset(_engine, asset):
        assets[(asset.draft_id, asset.slack_file_id)] = asset
        return len(assets)

    async def update_asset(_engine, asset):
        assets[(asset.draft_id, asset.slack_file_id)] = asset

    async def source_assets(_engine, draft_id):
        return [
            asset
            for (asset_draft_id, _), asset in assets.items()
            if asset_draft_id == draft_id and asset.role == "source"
        ]

    monkeypatch.setattr(video_workflow, "claim_video_asset", claim_asset)
    monkeypatch.setattr(video_workflow, "persist_video_asset", persist_asset)
    monkeypatch.setattr(video_workflow, "update_video_asset", update_asset)
    monkeypatch.setattr(video_workflow, "get_source_assets", source_assets)

    async def download(_client, upload, destination, _limit):
        path = destination / f"{upload.file_id}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        return path

    async def normalize(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        return _metadata(destination)

    async def frames(video, output_dir, count=6):
        output_dir.mkdir(parents=True, exist_ok=True)
        frame = output_dir / "frame-01.jpg"
        frame.write_bytes(b"jpeg")
        return [frame]

    monkeypatch.setattr(video_workflow, "download_slack_file", AsyncMock(side_effect=download))
    monkeypatch.setattr(
        video_workflow, "inspect_video", AsyncMock(side_effect=lambda path: _metadata(path))
    )
    monkeypatch.setattr(video_workflow, "normalize_video", AsyncMock(side_effect=normalize))
    monkeypatch.setattr(video_workflow, "extract_keyframes", AsyncMock(side_effect=frames))
    monkeypatch.setattr(video_workflow, "validate_media", lambda *_args: [])
    monkeypatch.setattr(
        video_workflow,
        "review_video",
        AsyncMock(return_value=VideoReview("pass", ["clear"], [], ["aligned"], [], "Ready")),
    )
    return assets, video_workflow


async def test_rejects_upload_outside_an_authorized_draft_thread(boundaries, tmp_path):
    _assets, workflow = boundaries
    workflow.find_thread_draft.return_value = None
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(object(), slack, object(), _upload(), _settings(tmp_path))

    assert result == WorkflowResult(
        "rejected", "This upload is not in an active TikTok draft thread.", None
    )
    slack.chat_postMessage.assert_awaited_once_with(
        channel="C123", thread_ts="1710000000.123456", text=result.message
    )
    workflow.download_slack_file.assert_not_awaited()


async def test_lookup_failure_replies_with_a_failed_workflow_result(boundaries, tmp_path):
    _assets, workflow = boundaries
    workflow.find_thread_draft.side_effect = RuntimeError("database unavailable")
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(object(), slack, object(), _upload(), _settings(tmp_path))

    assert result.status == "failed"
    assert "database unavailable" in result.message
    slack.chat_postMessage.assert_awaited_once_with(
        channel="C123", thread_ts="1710000000.123456", text=result.message
    )


async def test_draft_file_lock_serializes_contending_workers(tmp_path):
    import asyncio

    entered: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def first_worker():
        async with _draft_file_lock(tmp_path / "draft-42"):
            entered.append("first")
            first_entered.set()
            await release_first.wait()

    async def second_worker():
        async with _draft_file_lock(tmp_path / "draft-42"):
            entered.append("second")

    first = asyncio.create_task(first_worker())
    await first_entered.wait()
    second = asyncio.create_task(second_worker())
    await asyncio.sleep(0)
    assert entered == ["first"]

    release_first.set()
    await asyncio.gather(first, second)
    assert entered == ["first", "second"]


async def test_rejects_unauthorized_uploader_even_when_routed_to_a_valid_thread(
    boundaries, tmp_path
):
    _assets, workflow = boundaries
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(
        object(), slack, object(), _upload(user_id="UINTRUDER"), _settings(tmp_path)
    )

    assert result.status == "rejected"
    assert "authorized" in result.message
    workflow.download_slack_file.assert_not_awaited()


async def test_duplicate_upload_returns_prior_result_without_second_download(boundaries, tmp_path):
    assets, workflow = boundaries
    slack = SimpleNamespace(chat_postMessage=AsyncMock())
    upload = _upload()

    first = await process_video_upload(object(), slack, object(), upload, _settings(tmp_path))
    second = await process_video_upload(object(), slack, object(), upload, _settings(tmp_path))

    assert first.status == "reviewed"
    assert second == first
    assert len(assets) == 1
    assert workflow.download_slack_file.await_count == 1


async def test_invalid_media_replies_and_removes_failed_intermediates(boundaries, tmp_path):
    _assets, workflow = boundaries
    workflow.validate_media = lambda *_args: ["Video must be vertical."]
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(object(), slack, object(), _upload(), _settings(tmp_path))

    assert result.status == "rejected"
    assert "Video must be vertical." in result.message
    assert not list((_settings(tmp_path).video_media_root / "draft-42" / "work").glob("**/*"))
    workflow.review_video.assert_not_awaited()


async def test_one_clip_persists_source_reviews_normalized_video_and_replies(boundaries, tmp_path):
    assets, workflow = boundaries
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(object(), slack, object(), _upload(), _settings(tmp_path))

    assert result.status == "reviewed"
    assert result.output_path is not None and result.output_path.exists()
    assert assets[(42, "F001")].role == "source"
    assert assets[(42, "F001")].status == "reviewed"
    workflow.review_video.assert_awaited_once()
    assert "Ready" in slack.chat_postMessage.await_args.kwargs["text"]


async def test_two_clips_combine_normalized_sources_in_upload_order(boundaries, tmp_path):
    _assets, workflow = boundaries
    slack = SimpleNamespace(chat_postMessage=AsyncMock())
    combined = []

    async def combine(paths, output):
        combined.append(paths)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"combined")
        return _metadata(output)

    workflow.combine_video_clips = combine
    await process_video_upload(
        object(), slack, object(), _upload(file_id="F001"), _settings(tmp_path)
    )
    result = await process_video_upload(
        object(), slack, object(), _upload(file_id="F002"), _settings(tmp_path)
    )

    assert result.status == "combined"
    assert [path.name for path in combined[0]] == ["F001-normalized.mp4", "F002-normalized.mp4"]
    assert result.output_path is not None and result.output_path.exists()


async def test_missing_accepted_source_returns_retry_failure_without_normalizing_it(
    boundaries, tmp_path
):
    assets, workflow = boundaries
    assets[(42, "F-in-flight")] = VideoAsset(
        draft_id=42,
        slack_file_id="F-in-flight",
        thread_ts="1710000000.123456",
        path=str(tmp_path / "missing-source.mp4"),
        role="source",
        mime_type="video/mp4",
        size_bytes=100,
        duration_seconds=None,
        width=None,
        height=None,
        status="accepted",
        review={},
    )
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    result = await process_video_upload(
        object(), slack, object(), _upload(file_id="F002"), _settings(tmp_path)
    )

    assert result.status == "failed"
    assert "retry" in result.message.lower()
    normalized_sources = [call.args[0] for call in workflow.normalize_video.await_args_list]
    assert tmp_path / "missing-source.mp4" not in normalized_sources
    assert assets[(42, "F002")].status == "failed"


async def test_processing_failure_removes_partial_output_and_replies(boundaries, tmp_path):
    _assets, workflow = boundaries
    slack = SimpleNamespace(chat_postMessage=AsyncMock())

    async def exploding_normalize(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"partial")
        raise RuntimeError("ffmpeg failed")

    workflow.normalize_video = exploding_normalize
    result = await process_video_upload(object(), slack, object(), _upload(), _settings(tmp_path))

    assert result.status == "failed"
    assert "ffmpeg failed" in result.message
    assert not list((_settings(tmp_path).video_media_root / "draft-42" / "work").glob("**/*"))
    assert "could not be processed" in slack.chat_postMessage.await_args.kwargs["text"]
