"""Check that keepalive restores browser state without losing manual replacements."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from data.plugins.astrbot_plugin_link_resolver.core.common import playwright_cookies


class TestBrowserCookieState(unittest.IsolatedAsyncioTestCase):
    def setup_browser(self, value="renewed", failure=False):
        cookies = [{"name": "SUB", "value": value, "domain": ".weibo.com", "path": "/"}]
        self.context = Mock()
        self.context.add_cookies = AsyncMock()
        self.context.cookies = AsyncMock(return_value=cookies)
        self.context.storage_state = AsyncMock(return_value={
            "cookies": cookies + [{"name": "SSO", "value": "sso-state", "domain": ".sina.com.cn", "path": "/"}],
            "origins": [{"origin": "https://weibo.com", "localStorage": [{"name": "session", "value": "state"}]}],
        })
        page = Mock()
        page.goto = AsyncMock(return_value=None, side_effect=RuntimeError("timeout") if failure else None)
        page.wait_for_load_state = AsyncMock()
        page.close = AsyncMock()
        self.context.new_page = AsyncMock(return_value=page)
        self.context.close = AsyncMock()
        self.browser = Mock(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        runtime = AsyncMock()
        runtime.__aenter__.return_value = Mock()
        module = types.ModuleType("playwright.async_api")
        module.async_playwright = Mock(return_value=runtime)
        return patch.dict(sys.modules, {"playwright.async_api": module})

    async def collect(self, path, cookie="old"):
        with patch.object(playwright_cookies, "launch_chromium", new=AsyncMock(return_value=self.browser)), \
             patch.object(playwright_cookies, "configure_playwright_browser_path"), \
             patch.object(playwright_cookies, "browser_channel_candidates", return_value=[]):
            return await playwright_cookies.collect_browser_cookies(
                cookies={"SUB": cookie}, refresh_urls=("https://weibo.com/",),
                allowed_domains=("weibo.com",), cookie_domain=".weibo.com",
                user_agent="test", timeout_ms=1000, state_path=path,
                session_cookie_names=("SUB",),
            )

    async def test_restores_scoped_cookies_and_local_storage_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            self.context.add_cookies.assert_awaited_once()
            saved = json.loads(path.read_text(encoding="utf-8"))
            with self.setup_browser():
                await self.collect(path, cookie="renewed")
            self.context.add_cookies.assert_not_awaited()
            self.assertEqual(self.browser.new_context.call_args.kwargs["storage_state"], saved["storage_state"])
            self.assertEqual(saved["storage_state"]["cookies"][1]["domain"], ".sina.com.cn")

    async def test_manual_replacement_does_not_restore_previous_account_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            with self.setup_browser(value="manual"):
                await self.collect(path, cookie="manual")
            self.assertNotIn("storage_state", self.browser.new_context.call_args.kwargs)
            self.assertEqual(self.context.add_cookies.call_args.args[0][0]["value"], "manual")

    async def test_failed_navigation_preserves_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            saved = path.read_text(encoding="utf-8")
            with self.setup_browser(failure=True):
                with self.assertRaisesRegex(RuntimeError, "No browser keepalive page loaded"):
                    await self.collect(path)
            self.assertEqual(path.read_text(encoding="utf-8"), saved)
            self.context.close.assert_awaited_once()
            self.browser.close.assert_awaited_once()

    async def test_deleted_login_cookie_does_not_save_browser_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                self.context.cookies.return_value = []
                with self.assertRaisesRegex(RuntimeError, "removed login cookies"):
                    await self.collect(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
