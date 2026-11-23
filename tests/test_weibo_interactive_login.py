# ruff: noqa: E402
"""Tests for Weibo interactive Playwright login, expiry notifications, and Netscape persistence."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.common.playwright_cookies import (
    BrowserCookieRefreshError,
)
from data.plugins.astrbot_plugin_link_resolver.core.weibo import (
    WeiboExtractor,
    WeiboInteractiveLoginManager,
    WeiboLoginCancelledError,
    WeiboLoginCaptchaBlockedError,
)
from data.plugins.astrbot_plugin_link_resolver.core.weibo import interactive_login


class _FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        on_click=None,
        on_fill=None,
        tag_name: str = "img",
    ):
        self._visible = visible
        self._on_click = on_click
        self._on_fill = on_fill
        self._tag_name = tag_name
        self.filled_values: list[str] = []
        self.clicks = 0
        self.first = self

    async def is_visible(self, timeout=None):
        return self._visible

    async def click(self):
        self.clicks += 1
        if callable(self._on_click):
            res = self._on_click()
            if asyncio.iscoroutine(res):
                await res

    async def fill(self, value: str):
        self.filled_values.append(value)
        if callable(self._on_fill):
            res = self._on_fill(value)
            if asyncio.iscoroutine(res):
                await res

    async def evaluate(self, script: str):
        if "tagName" in script:
            return self._tag_name
        return True

    async def screenshot(self, path: str):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nFAKEQR")


class _FakePage:
    def __init__(self, url: str = interactive_login.WEIBO_SIGNIN_URL):
        self.url = url
        self.locators: dict[str, _FakeLocator] = {}
        self.body_text = ""
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_load_state = AsyncMock()
        self.close = AsyncMock()

    async def _goto(self, url: str, **_kwargs):
        self.url = url
        return None

    def locator(self, selector: str):
        return self.locators.get(selector, _FakeLocator(visible=False))

    async def inner_text(self, selector: str, timeout=None):
        return self.body_text

    async def screenshot(self, path: str, full_page: bool = False):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nFULLPAGE")


def _patch_playwright(page: _FakePage, context: Mock):
    browser = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
    runtime = AsyncMock()
    runtime.__aenter__.return_value = Mock()
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = Mock(return_value=runtime)
    return browser, patch.dict(sys.modules, {"playwright.async_api": module})


class TestWeiboInteractiveLogin(unittest.IsolatedAsyncioTestCase):
    async def test_login_by_qr_captures_qr_and_collects_weibo_and_sina_cookies(self):
        page = _FakePage()
        page.locators["img[src*='qrcode']"] = _FakeLocator(visible=True, tag_name="img")

        final_cookies = [
            {
                "name": "SUB",
                "value": "sub-qr-token",
                "domain": ".weibo.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
            {
                "name": "SUBP",
                "value": "subp-qr-token",
                "domain": ".weibo.com",
                "path": "/",
                "secure": False,
                "httpOnly": False,
                "expires": 2000000000,
            },
            {
                "name": "tgc",
                "value": "sina-tgc-ticket",
                "domain": ".login.sina.com.cn",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
            {
                "name": "unrelated",
                "value": "ignore",
                "domain": ".example.com",
                "path": "/",
            },
        ]
        context = Mock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        context.cookies = AsyncMock(return_value=final_cookies)
        context.storage_state = AsyncMock(
            return_value={"cookies": final_cookies[:3], "origins": []}
        )

        qr_seen: list[bytes] = []
        qr_paths: list[Path] = []

        async def on_qr_ready(qr_path: Path):
            qr_paths.append(qr_path)
            qr_seen.append(qr_path.read_bytes())
            # Simulate user scanning the QR code and Weibo redirecting home.
            page.url = "https://weibo.com/"

        manager = WeiboInteractiveLoginManager()
        browser, pw_patch = _patch_playwright(page, context)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                pw_patch,
                patch.object(
                    interactive_login, "launch_chromium", new=AsyncMock(return_value=browser)
                ),
                patch.object(interactive_login, "configure_playwright_browser_path"),
                patch.object(
                    interactive_login, "browser_channel_candidates", return_value=[]
                ),
            ):
                result = await manager.login_by_qr(
                    owner_umo="qq:FriendMessage:10001",
                    qr_dir=Path(tmpdir),
                    on_qr_ready=on_qr_ready,
                    timeout_sec=10.0,
                    poll_interval_sec=0.05,
                )

            self.assertEqual(len(qr_seen), 1)
            self.assertTrue(qr_seen[0].startswith(b"\x89PNG"))
            # Temporary QR screenshot must be cleaned up after login completes.
            self.assertFalse(qr_paths[0].exists())
            self.assertEqual(
                result.desktop_cookies,
                {"SUB": "sub-qr-token", "SUBP": "subp-qr-token"},
            )
            self.assertEqual(result.sina_cookie_names, ["tgc"])
            self.assertEqual(len(result.cookies), 3)
            self.assertFalse(manager.is_active())

    async def test_login_by_code_submits_account_password_requests_code_and_logs_in(self):
        page = _FakePage()
        code_input = _FakeLocator(visible=False, tag_name="input")
        send_code_btn = _FakeLocator(visible=False, tag_name="button")

        def on_pwd_submit():
            code_input._visible = True
            send_code_btn._visible = True
            acct_input._visible = False
            pwd_input._visible = False

        def on_code_submit():
            if code_input.filled_values == ["654321"]:
                page.url = "https://weibo.com/"

        acct_input = _FakeLocator(visible=True, tag_name="input")
        pwd_input = _FakeLocator(visible=True, tag_name="input")

        def on_submit_click():
            if acct_input._visible:
                on_pwd_submit()
            else:
                on_code_submit()

        submit_btn = _FakeLocator(visible=True, on_click=on_submit_click, tag_name="button")
        page.locators["input[name='username']"] = acct_input
        page.locators["input[name='password']"] = pwd_input
        page.locators["button[type='submit']"] = submit_btn
        page.locators["input[name='code']"] = code_input
        page.locators["button:has-text('获取验证码')"] = send_code_btn

        final_cookies = [
            {
                "name": "SUB",
                "value": "sub-code-token",
                "domain": ".weibo.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
            {
                "name": "tgc",
                "value": "sina-code-tgc",
                "domain": ".login.sina.com.cn",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
        ]
        context = Mock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        context.cookies = AsyncMock(return_value=final_cookies)
        context.storage_state = AsyncMock(
            return_value={"cookies": final_cookies, "origins": []}
        )

        manager = WeiboInteractiveLoginManager()
        prompts: list[str] = []

        async def on_code_requested(prompt: str):
            prompts.append(prompt)
            # Simulate admin replying with the SMS verification code shortly after.
            asyncio.get_running_loop().call_soon(
                manager.submit_code, "654321", "qq:FriendMessage:10001"
            )

        browser, pw_patch = _patch_playwright(page, context)
        with (
            pw_patch,
            patch.object(
                interactive_login, "launch_chromium", new=AsyncMock(return_value=browser)
            ),
            patch.object(interactive_login, "configure_playwright_browser_path"),
            patch.object(
                interactive_login, "browser_channel_candidates", return_value=[]
            ),
        ):
            result = await manager.login_by_code(
                account="13800138000",
                password="secret-password",
                owner_umo="qq:FriendMessage:10001",
                on_code_requested=on_code_requested,
                timeout_sec=10.0,
                poll_interval_sec=0.05,
            )

        self.assertEqual(acct_input.filled_values, ["13800138000"])
        self.assertEqual(pwd_input.filled_values, ["secret-password"])
        self.assertEqual(send_code_btn.clicks, 1)
        self.assertEqual(code_input.filled_values, ["654321"])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result.desktop_cookies, {"SUB": "sub-code-token"})
        self.assertEqual(result.sina_cookie_names, ["tgc"])
        self.assertFalse(manager.is_active())

    async def test_login_by_code_aborts_cleanly_on_captcha_challenge(self):
        page = _FakePage()
        page.locators[".geetest_panel"] = _FakeLocator(visible=True)
        context = Mock(new_page=AsyncMock(return_value=page), close=AsyncMock())

        manager = WeiboInteractiveLoginManager()
        browser, pw_patch = _patch_playwright(page, context)
        with (
            pw_patch,
            patch.object(
                interactive_login, "launch_chromium", new=AsyncMock(return_value=browser)
            ),
            patch.object(interactive_login, "configure_playwright_browser_path"),
            patch.object(
                interactive_login, "browser_channel_candidates", return_value=[]
            ),
        ):
            with self.assertRaises(WeiboLoginCaptchaBlockedError):
                await manager.login_by_code(
                    account="13800138000",
                    password="secret",
                    owner_umo="qq:FriendMessage:10001",
                    on_code_requested=AsyncMock(),
                    timeout_sec=5.0,
                    poll_interval_sec=0.05,
                )
        self.assertFalse(manager.is_active())

    async def test_login_by_code_can_be_cancelled(self):
        page = _FakePage()
        page.locators["input[name='username']"] = _FakeLocator(visible=True)
        page.locators["input[name='code']"] = _FakeLocator(visible=True)
        context = Mock(new_page=AsyncMock(return_value=page), close=AsyncMock())

        manager = WeiboInteractiveLoginManager()

        async def on_code_requested(_prompt: str):
            asyncio.get_running_loop().call_soon(manager.cancel)

        browser, pw_patch = _patch_playwright(page, context)
        with (
            pw_patch,
            patch.object(
                interactive_login, "launch_chromium", new=AsyncMock(return_value=browser)
            ),
            patch.object(interactive_login, "configure_playwright_browser_path"),
            patch.object(
                interactive_login, "browser_channel_candidates", return_value=[]
            ),
        ):
            with self.assertRaises(WeiboLoginCancelledError):
                await manager.login_by_code(
                    account="13800138000",
                    password="",
                    owner_umo="qq:FriendMessage:10001",
                    on_code_requested=on_code_requested,
                    timeout_sec=5.0,
                    poll_interval_sec=0.05,
                )
        self.assertFalse(manager.is_active())

    async def test_apply_interactive_login_result_writes_full_netscape_and_browser_state(self):
        extractor = WeiboExtractor()
        callbacks: list[str] = []
        browser_cookies = [
            {
                "name": "SUB",
                "value": "desktop-sub",
                "domain": ".weibo.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
            {
                "name": "tgc",
                "value": "sina-tgc",
                "domain": ".login.sina.com.cn",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
        ]
        storage_state = {"cookies": browser_cookies, "origins": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = Path(tmpdir) / "weibo_cookies.txt"
            extractor.set_cookie_storage(cookie_file, callbacks.append)
            summary = await extractor.apply_interactive_login_result(
                browser_cookies, storage_state
            )

            self.assertTrue(summary["saved"])
            self.assertTrue(summary["has_tgc"])
            self.assertEqual(summary["desktop_login_fields"], ["SUB"])
            self.assertEqual(summary["sina_cookie_names"], ["tgc"])
            saved_text = cookie_file.read_text(encoding="utf-8")
            self.assertIn("# Netscape HTTP Cookie File", saved_text)
            self.assertIn("#HttpOnly_.weibo.com\tTRUE\t/\tTRUE\t2000000000\tSUB\tdesktop-sub", saved_text)
            self.assertIn("#HttpOnly_.login.sina.com.cn\tTRUE\t/\tTRUE\t2000000000\ttgc\tsina-tgc", saved_text)

            browser_state = json.loads(
                cookie_file.with_suffix(".browser.json").read_text(encoding="utf-8")
            )
            self.assertEqual(browser_state["storage_state"], storage_state)
            self.assertIsNotNone(browser_state["output_source_digest"])
            self.assertEqual(callbacks, [extractor.cookie])

    async def test_keepalive_preserves_sina_sso_cookies_in_netscape_file(self):
        extractor = WeiboExtractor()
        extractor.set_cookie(
            "\n".join(
                [
                    "# Netscape HTTP Cookie File",
                    "#HttpOnly_.weibo.com\tTRUE\t/\tTRUE\t2000000000\tSUB\told-sub",
                    "#HttpOnly_.login.sina.com.cn\tTRUE\t/\tTRUE\t2000000000\ttgc\told-tgc",
                ]
            )
        )
        refreshed_cookies = [
            {
                "name": "SUB",
                "value": "new-sub",
                "domain": ".weibo.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
            {
                "name": "tgc",
                "value": "new-tgc",
                "domain": ".login.sina.com.cn",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 2000000000,
            },
        ]
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = Path(tmpdir) / "weibo_cookies.txt"
            extractor.set_cookie_storage(cookie_file)
            with patch(
                module + ".collect_browser_cookies",
                new=AsyncMock(return_value=refreshed_cookies),
            ) as browser:
                await extractor._refresh_user_cookies()

            self.assertTrue(browser.call_args.kwargs["collect_all_cookies"])
            self.assertIn("sina.com.cn", browser.call_args.kwargs["allowed_domains"])
            persisted = cookie_file.read_text(encoding="utf-8")
            self.assertIn("new-sub", persisted)
            self.assertIn("new-tgc", persisted)
            self.assertIn(".login.sina.com.cn", persisted)

    async def test_expired_callback_fires_on_keepalive_failure_and_deduplicates_notification(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=expired-sub")
        reasons: list[str] = []

        async def on_expired(reason: str):
            reasons.append(reason)

        extractor.set_cookie_expired_callback(on_expired)
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        with patch(
            module + ".collect_browser_cookies",
            new=AsyncMock(side_effect=BrowserCookieRefreshError("stuck on passport")),
        ):
            await extractor._refresh_user_cookies()

        self.assertEqual(len(reasons), 1)
        self.assertIn("/weibo_login_scan", reasons[0])

        # Verify LinkResolverPlugin._on_weibo_cookie_expired sends proactive message & deduplicates.
        source = Path(__file__).resolve().parents[1] / "main.py"
        plugin_cls = next(
            node
            for node in ast.parse(source.read_text(encoding="utf-8")).body
            if isinstance(node, ast.ClassDef) and node.name == "LinkResolverPlugin"
        )
        methods = [
            node
            for node in plugin_cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {
                "_parse_weibo_notification_targets",
                "_send_proactive_message",
                "_on_weibo_cookie_expired",
            }
        ]
        sent_messages: list[tuple[str, object]] = []

        class _Plain:
            def __init__(self, text):
                self.text = text

        class _Chain(list):
            pass

        namespace = {
            "asyncio": asyncio,
            "hashlib": hashlib,
            "re": __import__("re"),
            "time": time,
            "Comp": SimpleNamespace(Plain=_Plain, BaseMessageComponent=object),
            "MessageChain": _Chain,
            "WEIBO_EXPIRE_NOTIFY_COOLDOWN_SEC": 3600,
            "logger": Mock(),
        }
        ast_cls = ast.ClassDef(
            name="NotifyHarness", bases=[], keywords=[], body=methods, decorator_list=[]
        )
        exec(
            compile(ast.fix_missing_locations(ast.Module(body=[ast_cls], type_ignores=[])), str(source), "exec"),
            namespace,
        )
        harness = namespace["NotifyHarness"]()
        harness.weibo_cookie_notification_target = "qq:FriendMessage:10001\nqq:FriendMessage:10002"
        harness.weibo_extractor = extractor
        harness._weibo_cookie_expired_notified_fingerprint = None
        harness._weibo_cookie_last_notify_at = 0.0

        async def send_message(target, chain):
            sent_messages.append((target, chain))
            return True

        harness.context = SimpleNamespace(send_message=send_message)

        await harness._on_weibo_cookie_expired("Cookie 已失效")
        await harness._on_weibo_cookie_expired("Cookie 已失效")  # deduplicated

        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0][0], "qq:FriendMessage:10001")
        self.assertEqual(sent_messages[1][0], "qq:FriendMessage:10002")
        self.assertIn("/weibo_login_scan", sent_messages[0][1][0].text)
        self.assertIn("/weibo_login_code", sent_messages[0][1][0].text)

    async def test_is_user_cookie_valid_checks_weibo_config_login_flag(self):
        import httpx

        extractor = WeiboExtractor()
        self.assertFalse(await extractor.is_user_cookie_valid())

        extractor.set_cookie("SUB=valid-sub")
        valid_resp = httpx.Response(200, json={"ok": 1, "data": {"login": True, "uid": "123"}})
        invalid_resp = httpx.Response(200, json={"ok": 1, "data": {"login": False}})
        unauth_resp = httpx.Response(403, text="Forbidden")

        for resp, expected in (
            (valid_resp, True),
            (invalid_resp, False),
            (unauth_resp, False),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(return_value=resp)
            with patch(
                "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                return_value=mock_client,
            ):
                self.assertEqual(await extractor.is_user_cookie_valid(), expected)

    async def test_login_commands_skip_when_cookies_are_valid(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        plugin_cls = next(
            node
            for node in ast.parse(source.read_text(encoding="utf-8")).body
            if isinstance(node, ast.ClassDef) and node.name == "LinkResolverPlugin"
        )
        methods = []
        for node in plugin_cls.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {
                "cmd_weibo_login_scan",
                "cmd_weibo_login_code",
            }:
                node.decorator_list = []
                methods.append(node)

        namespace = {
            "asyncio": asyncio,
            "AstrMessageEvent": object,
        }
        ast_cls = ast.ClassDef(
            name="CommandHarness", bases=[], keywords=[], body=methods, decorator_list=[]
        )
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[ast_cls], type_ignores=[])),
                str(source),
                "exec",
            ),
            namespace,
        )
        harness = namespace["CommandHarness"]()
        harness._refresh_config = Mock()
        harness.weibo_login_manager = SimpleNamespace(is_active=lambda: False)
        harness._weibo_login_task = None
        harness.weibo_login_account = ""
        harness.weibo_extractor = SimpleNamespace(
            is_user_cookie_valid=AsyncMock(return_value=True)
        )
        harness._run_weibo_qr_login_task = AsyncMock()
        harness._run_weibo_code_login_task = AsyncMock()

        event = SimpleNamespace(
            unified_msg_origin="qq:FriendMessage:10001",
            plain_result=lambda text: text,
        )

        scan_outputs = [item async for item in harness.cmd_weibo_login_scan(event)]
        code_outputs = [item async for item in harness.cmd_weibo_login_code(event)]

        self.assertEqual(scan_outputs, ["✅ 当前微博 Cookie 仍有效，无需重新登录。"])
        self.assertEqual(code_outputs, ["✅ 当前微博 Cookie 仍有效，无需重新登录。"])
        self.assertIsNone(harness._weibo_login_task)


if __name__ == "__main__":
    unittest.main()
