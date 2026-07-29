# ruff: noqa: E402
"""Tests for plugin-owned Playwright browser installation."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.common import playwright_manager


class FakeChromium:
    def __init__(self):
        self.calls = 0

    async def launch(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("missing browser")
        return {"kwargs": kwargs}


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class TestPlaywrightManager(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        playwright_manager._INSTALL_ATTEMPTED = False

    def test_configure_browser_path_uses_plugin_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "playwright-browsers"
            with patch.object(
                playwright_manager,
                "get_playwright_browsers_path",
                return_value=target,
            ):
                result = playwright_manager.configure_playwright_browser_path()

        self.assertEqual(result, target)
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(target))

    def test_launch_installs_chromium_after_missing_browser(self):
        fake = FakePlaywright()

        async def run():
            with (
                patch.object(
                    playwright_manager,
                    "configure_playwright_browser_path",
                    return_value=Path("/tmp/pw"),
                ),
                patch.object(
                    playwright_manager,
                    "ensure_chromium_installed",
                    new=AsyncMock(return_value=True),
                ) as install_mock,
            ):
                browser = await playwright_manager.launch_chromium(fake, headless=True)
            install_mock.assert_awaited_once()
            return browser

        browser = asyncio.run(run())

        self.assertEqual(fake.chromium.calls, 2)
        self.assertEqual(browser["kwargs"], {"headless": True})

    def test_install_chromium_uses_python_module_and_plugin_browser_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            browsers_path = Path(tmpdir) / "playwright-browsers"
            completed = Mock(returncode=0, stdout="")
            with patch.object(
                playwright_manager.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                ok = playwright_manager._install_chromium(browsers_path)

        self.assertTrue(ok)
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "playwright", "install", "chromium"])
        self.assertEqual(kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"], str(browsers_path))


if __name__ == "__main__":
    unittest.main()
