"""Playwright-based screenshots of public web pages.

Used by the creative pipeline to capture peermarket.eu pages for use as
Meta/TikTok ad assets. Always headless, always Chromium, no JS evaluation
beyond what the page itself runs.
"""
import structlog
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from playwright.async_api import (
    async_playwright,
)

log = structlog.get_logger(__name__)


class ScreenshotError(RuntimeError):
    """Raised when a screenshot attempt fails (timeout, navigation error, etc.)."""


async def screenshot_url(
    url: str,
    *,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    wait_for: str = "networkidle",
    full_page: bool = False,
    timeout_ms: int = 30_000,
) -> bytes:
    """Render `url` in headless Chromium and return the PNG bytes.

    Args:
        url: Fully-qualified http(s) URL to capture.
        viewport_width / viewport_height: Browser viewport size in CSS pixels.
        wait_for: Playwright wait_until strategy ('load', 'domcontentloaded', 'networkidle').
        full_page: If True, capture the entire scroll height (not just the viewport).
        timeout_ms: Page-load timeout. Past this, ScreenshotError is raised.

    Raises ScreenshotError on any Playwright failure with the underlying message preserved.
    """
    log.info("screenshot.start", url=url, viewport=(viewport_width, viewport_height))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport={"width": viewport_width, "height": viewport_height},
                    device_scale_factor=2,  # retina for sharper text
                )
                page = await context.new_page()
                await page.goto(url, wait_until=wait_for, timeout=timeout_ms)
                png = await page.screenshot(full_page=full_page, type="png")
            finally:
                await browser.close()
    except PlaywrightTimeoutError as e:
        raise ScreenshotError(f"timeout loading {url!r} after {timeout_ms}ms") from e
    except PlaywrightError as e:
        raise ScreenshotError(f"playwright error loading {url!r}: {e}") from e
    log.info("screenshot.success", url=url, bytes=len(png))
    return png
