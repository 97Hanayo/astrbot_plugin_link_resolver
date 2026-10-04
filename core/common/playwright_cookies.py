"""Browser-backed Cookie refresh helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from .cookies import atomic_write_cookie_text, host_matches
from .playwright_manager import (
    browser_channel_candidates,
    configure_playwright_browser_path,
    launch_chromium,
)


class BrowserCookieRefreshError(RuntimeError):
    """A browser ran, but the page did not complete the expected recovery flow."""


async def collect_browser_cookies(
    *,
    cookies: Mapping[str, str],
    refresh_urls: Sequence[str],
    allowed_domains: Iterable[str],
    user_agent: str | None,
    timeout_ms: int,
    viewport: Mapping[str, int] | None = None,
    cookie_domain: str | None = None,
    state_path: Path | None = None,
    session_cookie_names: Sequence[str] = (),
    return_url_pattern: str | None = None,
    on_navigation: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Load cookies in Chromium and return the final same-site Cookie jar.

    Playwright exposes the browser's effective Cookie jar, which includes both
    HTTP ``Set-Cookie`` updates and cookies written by page scripts. Values are
    returned to the caller so each platform can apply its login-cookie safety
    rules before persistence.
    """
    if not cookies or not refresh_urls:
        return []

    def fingerprint(values: Mapping[str, str]) -> str:
        selected = {name: values.get(name, "") for name in session_cookie_names}
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()

    input_digest = fingerprint(cookies)
    saved_state = None
    if state_path is not None:
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if input_digest in (saved.get("input_digest"), saved.get("output_digest")):
                candidate_state = saved.get("storage_state")
                if isinstance(candidate_state, dict):
                    saved_state = candidate_state
        except (OSError, ValueError, AttributeError):
            pass

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
            if saved_state is not None:
                context_options["storage_state"] = saved_state
            context = await browser.new_context(**context_options)
            try:
                seed_cookies = [
                    {
                        "name": name,
                        "value": value,
                        **({"domain": cookie_domain, "path": "/", "secure": True}
                           if cookie_domain else {"url": refresh_url}),
                    }
                    for refresh_url in (refresh_urls[:1] if cookie_domain else refresh_urls)
                    for name, value in cookies.items()
                    if name and value
                ]
                if seed_cookies and saved_state is None:
                    await context.add_cookies(seed_cookies)

                loaded = False
                for refresh_url in refresh_urls:
                    page = await context.new_page()
                    if on_navigation is not None:
                        def navigated(frame):
                            if frame == page.main_frame:
                                on_navigation(frame.url)
                        page.on("framenavigated", navigated)
                    try:
                        try:
                            response = await page.goto(
                                refresh_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, int(timeout_ms)),
                            )
                            if state_path is not None and response is not None and response.status >= 400:
                                raise RuntimeError("Browser keepalive page failed")
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
                            if return_url_pattern is not None:
                                try:
                                    await page.wait_for_url(
                                        re.compile(return_url_pattern),
                                        wait_until="domcontentloaded",
                                        timeout=max(1000, int(timeout_ms)),
                                    )
                                except Exception as exc:
                                    raise BrowserCookieRefreshError(
                                        "Browser did not return to the expected homepage"
                                    ) from exc
                            loaded = True
                    finally:
                        await page.close()

                if (state_path is not None or return_url_pattern is not None) and not loaded:
                    raise BrowserCookieRefreshError("No browser keepalive page loaded")
                allowed = tuple(allowed_domains)
                collected = (
                    await context.cookies(list(refresh_urls))
                    if return_url_pattern is not None else await context.cookies()
                )
                browser_cookies = [
                    cookie
                    for cookie in collected
                    if host_matches(str(cookie.get("domain") or ""), allowed)
                ]
                if state_path is not None:
                    effective = {
                        item["name"]: item["value"]
                        for item in await context.cookies(list(refresh_urls))
                    }
                    if any(
                        cookies.get(name) and not effective.get(name)
                        for name in session_cookie_names
                    ):
                        raise BrowserCookieRefreshError("Browser keepalive removed login cookies")
                    # Preserve scoped cookies and local storage, including SSO
                    # state from redirects. Never flatten that state into headers.
                    atomic_write_cookie_text(state_path, json.dumps({
                        "input_digest": input_digest,
                        "output_digest": fingerprint(effective),
                        "storage_state": await context.storage_state(),
                    }))
                return browser_cookies
            finally:
                await context.close()
        finally:
            await browser.close()


__all__ = ["collect_browser_cookies"]
