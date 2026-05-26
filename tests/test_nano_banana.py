"""Nano Banana wrapper tests — no real API calls."""

from unittest.mock import MagicMock

import pytest

from peermarket_agent.nano_banana import (
    ImageEditDisabled,
    ImageEditError,
    edit_image,
)


def _build_response_with_image(image_bytes: bytes):
    """Build a mock that mimics google-genai's response structure."""
    inline = MagicMock()
    inline.data = image_bytes
    part = MagicMock()
    part.inline_data = inline
    part.text = None
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    response = MagicMock()
    response.candidates = [candidate]
    return response


def _build_response_with_text_refusal(text: str):
    """Mock the refusal case — Nano Banana sends text instead of image."""
    part = MagicMock()
    part.inline_data = None
    part.text = text
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    response = MagicMock()
    response.candidates = [candidate]
    return response


def _patch_genai_client(monkeypatch, response):
    """Patch the genai.Client to return a stub model.generate_content."""
    fake_client = MagicMock()
    fake_client.models.generate_content = MagicMock(return_value=response)
    monkeypatch.setattr(
        "peermarket_agent.nano_banana.genai.Client",
        lambda *a, **kw: fake_client,
    )
    return fake_client


async def test_edit_image_returns_png_bytes(monkeypatch):
    fake_client = _patch_genai_client(monkeypatch, _build_response_with_image(b"NEW_PNG"))
    result = await edit_image(
        api_key="x",
        image_bytes=b"OLD_PNG",
        prompt="add a green brand frame",
    )
    assert result == b"NEW_PNG"
    fake_client.models.generate_content.assert_called_once()


async def test_edit_image_includes_guardrails_in_prompt(monkeypatch):
    fake_client = _patch_genai_client(monkeypatch, _build_response_with_image(b"X"))
    await edit_image(api_key="x", image_bytes=b"Y", prompt="add a brand frame")
    args, kwargs = fake_client.models.generate_content.call_args
    contents = kwargs.get("contents") or (args[1] if len(args) > 1 else None)
    assert contents is not None
    # First element is the prompt string; verify it has the guardrails preamble
    prompt_text = contents[0]
    assert "Do NOT synthesize humans" in prompt_text
    assert "add a brand frame" in prompt_text


async def test_edit_image_without_api_key_raises_disabled():
    with pytest.raises(ImageEditDisabled, match="GEMINI_API_KEY"):
        await edit_image(api_key="", image_bytes=b"x", prompt="x")


async def test_edit_image_handles_text_refusal_as_error(monkeypatch):
    _patch_genai_client(
        monkeypatch,
        _build_response_with_text_refusal("I cannot create that image"),
    )
    with pytest.raises(ImageEditError, match="refused"):
        await edit_image(api_key="x", image_bytes=b"y", prompt="z")


async def test_edit_image_handles_empty_response(monkeypatch):
    response = MagicMock()
    response.candidates = []
    _patch_genai_client(monkeypatch, response)
    with pytest.raises(ImageEditError, match="no candidates"):
        await edit_image(api_key="x", image_bytes=b"y", prompt="z")


async def test_edit_image_handles_sdk_exception(monkeypatch):
    fake_client = MagicMock()
    fake_client.models.generate_content = MagicMock(side_effect=RuntimeError("nano banana down"))
    monkeypatch.setattr(
        "peermarket_agent.nano_banana.genai.Client",
        lambda *a, **kw: fake_client,
    )
    with pytest.raises(ImageEditError, match="nano banana down"):
        await edit_image(api_key="x", image_bytes=b"y", prompt="z")
