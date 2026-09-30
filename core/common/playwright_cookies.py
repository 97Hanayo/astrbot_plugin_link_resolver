"""Browser-backed Cookie refresh helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .cookies import host_matches
from .playwright_manager import (
    browser_channel_candidates,
    configure_playwright_browser_path,
    launch_chromium,
)


async def collect_browser_cookies(
    *,
    cookies: Mapping[str, str],
    refresh_urls: Sequence[str],
    allowed_domains: Iterable[str],
    user_agent: str | None,
    timeout_ms: int,
    viewport: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Load cookies in Chromium and return the final same-site Cookie jar.

    Playwright exposes the browser's effective Cookie jar, which includes both
    HTTP ``Set-Cookie`` updates and cookies written by page scripts. Values are
    returned to the caller so each platform can apply its login-cookie safety
    rules before persistence.
    """
    if not cookies or not refresh_urls:
        return []

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError("Playwright is unavailable") from exc

    configure_playwright_browser_path()
    async with async_playwright() as playwright:
        browser = await launch_chromium(
            playwright,
            headless=True,
            fallback_executable_paths=browser_channel_candidates(),
        )
        try:
            context_options = {
                "viewport": dict(viewport or {"width": 1280, "height": 900}),
                "locale": "zh-CN",
            }
            if user_agent:
                context_options["user_agent"] = user_agent
            context = await browser.new_context(**context_options)
            try:
                seed_cookies = [
                    {
                        "name": name,
                        "value": value,
                        "url": refresh_url,
                        "path": "/",
                    }
                    for refresh_url in refresh_urls
                    for name, value in cookies.items()
                    if name and value
                ]
                if seed_cookies:
                    await context.add_cookies(seed_cookies)

                for refresh_url in refresh_urls:
                    page = await context.new_page()
                    try:
                        try:
                            await page.goto(
                                refresh_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, int(timeout_ms)),
                            )
                        except Exception:
                            # Continue with another first-party refresh URL if
                            # one site variant is blocked or times out.
                            pass
                        else:
                            try:
                                await page.wait_for_load_state(
                                    "networkidle",
                                    timeout=min(max(1000, int(timeout_ms)), 8000),
                                )
                            except Exception:
                                # Trackers and long-polling requests can prevent
                                # networkidle without affecting Cookie updates.
                                pass
                    finally:
                        await page.close()

                allowed = tuple(allowed_domains)
                return [
                    cookie
                    for cookie in await context.cookies()
                    if host_matches(str(cookie.get("domain") or ""), allowed)
                ]
            finally:
                await context.close()
        finally:
            await browser.close()


__all__ = ["collect_browser_cookies"]
