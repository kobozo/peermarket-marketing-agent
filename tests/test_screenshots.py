"""Playwright screenshot tests — no real browser."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from peermarket_agent.screenshots import ScreenshotError, screenshot_url


def _build_fake_playwright(*, screenshot_bytes=b"PNG_DATA", raises=None):
    """Build a chain of mocks that mimics async_playwright()'s API surface."""
    page = AsyncMock()
    if raises is not None:
        page.goto = AsyncMock(side_effect=raises)
    else:
        page.goto = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=screenshot_bytes)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock(return_value=None)

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw = MagicMock()
    pw.chromium = chromium

    class _CM:
        async def __aenter__(self_inner):
            return pw

        async def __aexit__(self_inner, *args):
            return None

    cm = _CM()
    return cm, page, browser


async def test_screenshot_url_returns_png_bytes(monkeypatch):
    cm, page, browser = _build_fake_playwright(screenshot_bytes=b"fakepng")
    monkeypatch.setattr(
        "peermarket_agent.screenshots.async_playwright",
        lambda: cm,
    )
    result = await screenshot_url("https://peermarket.eu/")
    assert result == b"fakepng"
    page.goto.assert_awaited_once_with(
        "https://peermarket.eu/", wait_until="networkidle", timeout=30_000
    )
    page.screenshot.assert_awaited_once_with(full_page=False, type="png")
    browser.close.assert_awaited_once()


async def test_screenshot_url_passes_viewport(monkeypatch):
    cm, page, browser = _build_fake_playwright()
    captured = {}

    async def fake_new_context(**kwargs):
        captured["viewport"] = kwargs.get("viewport")
        # Re-attach the page chain expected by the caller
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        return ctx

    browser.new_context = fake_new_context
    monkeypatch.setattr(
        "peermarket_agent.screenshots.async_playwright",
        lambda: cm,
    )
    await screenshot_url("https://x", viewport_width=720, viewport_height=1280)
    assert captured["viewport"] == {"width": 720, "height": 1280}


async def test_screenshot_url_full_page_passes_flag(monkeypatch):
    cm, page, browser = _build_fake_playwright()
    monkeypatch.setattr(
        "peermarket_agent.screenshots.async_playwright",
        lambda: cm,
    )
    await screenshot_url("https://x", full_page=True)
    page.screenshot.assert_awaited_once_with(full_page=True, type="png")


async def test_screenshot_url_timeout_raises_screenshot_error(monkeypatch):
    from playwright.async_api import TimeoutError as PWTimeoutError

    cm, _, _ = _build_fake_playwright(raises=PWTimeoutError("timeout"))
    monkeypatch.setattr(
        "peermarket_agent.screenshots.async_playwright",
        lambda: cm,
    )
    with pytest.raises(ScreenshotError, match="timeout"):
        await screenshot_url("https://x", timeout_ms=1000)


async def test_screenshot_url_navigation_error_raises_screenshot_error(monkeypatch):
    from playwright.async_api import Error as PWError

    cm, _, _ = _build_fake_playwright(raises=PWError("net::ERR_NAME_NOT_RESOLVED"))
    monkeypatch.setattr(
        "peermarket_agent.screenshots.async_playwright",
        lambda: cm,
    )
    with pytest.raises(ScreenshotError, match="net::ERR"):
        await screenshot_url("https://does-not-exist.invalid")
