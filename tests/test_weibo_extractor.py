# ruff: noqa: E402
"""Unit tests for the Weibo extractor.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_weibo_extractor.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.weibo import (
    WeiboExtractor,
    extract_weibo_links,
)


class TestWeiboExtractor(unittest.IsolatedAsyncioTestCase):
    def test_extract_weibo_links_variants(self):
        text = (
            "看看这个 https://weibo.com/1234567890/AbCdEfGhI "
            "还有 m.weibo.cn/status/AbCdEfGhI 和 t.cn/A6abcXYZ"
        )

        links = extract_weibo_links(text)

        self.assertIn("https://weibo.com/1234567890/AbCdEfGhI", links)
        self.assertIn("https://m.weibo.cn/status/AbCdEfGhI", links)
        self.assertIn("https://t.cn/A6abcXYZ", links)

    async def test_user_cookie_is_preferred_over_visitor_cookie(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=foo; SUBP=bar")

        cookies = await extractor._get_request_cookies()

        self.assertEqual(cookies["SUB"], "foo")
        self.assertEqual(cookies["SUBP"], "bar")

    def test_parse_netscape_cookies_txt_for_weibo_domains(self):
        raw = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".weibo.com\tTRUE\t/\tTRUE\t0\tSUB\tweibo-sub",
                "#HttpOnly_.weibo.com\tTRUE\t/\tTRUE\t0\tSUBP\tweibo-subp",
                ".example.com\tTRUE\t/\tTRUE\t0\tignored\tvalue",
            ]
        )

        cookies = WeiboExtractor._parse_cookie_header(raw)

        self.assertEqual(cookies, {"SUB": "weibo-sub", "SUBP": "weibo-subp"})

    def test_parse_netscape_cookies_does_not_fall_back_to_multiline_header(self):
        raw = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".weibo.cn\tTRUE\t/\tTRUE\t0\tSUB\tmobile-sub",
                "",
                "# Netscape HTTP Cookie File",
                ".weibo.com\tTRUE\t/\tTRUE\t0\tSUB\tpc-sub",
                "weibo.com\tFALSE\t/\tTRUE\t0\tWBPSESS\tabc==",
            ]
        )

        cookies = WeiboExtractor._parse_cookie_header(raw)

        self.assertEqual(cookies, {"SUB": "pc-sub", "WBPSESS": "abc=="})
        self.assertTrue(all("\n" not in key + value for key, value in cookies.items()))

    def test_parse_cookie_header_ignores_unsafe_multiline_parts(self):
        raw = "Cookie: SUB=foo; SUBP=bar\nX-Other: no-cookie=value"

        cookies = WeiboExtractor._parse_cookie_header(raw)

        self.assertEqual(cookies, {"SUB": "foo", "SUBP": "bar"})

    def test_parse_visitor_jsonp_cookies(self):
        payload = (
            'window.visitor_gray_callback && visitor_gray_callback({"retcode":20000000,'
            '"msg":"succ","data":{"sub":"visitor-sub","subp":"visitor-subp"}});'
        )

        cookies = WeiboExtractor._parse_visitor_jsonp_cookies(payload)

        self.assertEqual(cookies, {"SUB": "visitor-sub", "SUBP": "visitor-subp"})

    async def test_server_cookie_updates_are_persisted_without_exposing_values(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old-sub; SUBP=old-subp")

        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_path = Path(tmpdir) / "cookies" / "weibo_cookies.txt"
            updates: list[str] = []
            extractor.set_cookie_storage(cookie_path, updates.append)
            response = httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", "SUB=new-sub; Domain=.weibo.com; Path=/; HttpOnly"),
                    ("Set-Cookie", "WBPSESS=session-1; Domain=.weibo.com; Path=/"),
                ],
                request=httpx.Request("GET", "https://weibo.com/", headers={"Cookie": extractor.cookie}),
            )

            real_client = httpx.AsyncClient
            def respond(request):
                self.assertEqual(str(request.url), "https://weibo.com/ajax/config/get_config")
                self.assertIn("SUB=new-sub", request.headers["Cookie"])
                return httpx.Response(200, json={"ok": 1, "data": {}})
            with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                       side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
                await extractor._capture_cookie_updates(response)

            self.assertEqual(
                extractor._user_cookies,
                {"SUB": "new-sub", "SUBP": "old-subp", "WBPSESS": "session-1"},
            )
            self.assertEqual(
                cookie_path.read_text("utf-8"),
                "SUB=new-sub; SUBP=old-subp; WBPSESS=session-1\n",
            )
            self.assertEqual(updates, [extractor.cookie])

    async def test_server_cookie_updates_ignore_non_weibo_responses_and_deletions(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old-sub; SUBP=old-subp")
        response = httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "SUB=; Max-Age=0; Domain=.weibo.com; Path=/"),
                ("Set-Cookie", "evil=changed; Domain=example.com; Path=/"),
            ],
            request=httpx.Request("GET", "https://example.com/"),
        )

        await extractor._capture_cookie_updates(response)

        self.assertEqual(extractor._user_cookies, {"SUB": "old-sub", "SUBP": "old-subp"})

        response = httpx.Response(
            200,
            headers={"Set-Cookie": "SUB=; Max-Age=0; Domain=.weibo.com; Path=/"},
            request=httpx.Request("GET", "https://weibo.com/", headers={"Cookie": extractor.cookie}),
        )
        await extractor._capture_cookie_updates(response)
        self.assertEqual(extractor._user_cookies, {"SUB": "old-sub", "SUBP": "old-subp"})

    def test_browser_cookie_candidate_is_desktop_only(self):
        extractor = WeiboExtractor()
        original = {"SUB": "desktop", "SUBP": "desktop-p"}
        candidate = extractor._browser_cookie_candidate([
            {"name": "SUB", "value": "new", "domain": ".weibo.com"},
            {"name": "SUBP", "value": "new-p", "domain": ".weibo.com"},
            {"name": "SUB", "value": "mobile", "domain": ".weibo.cn"},
            {"name": "SUBP", "value": "visitor", "domain": "passport.weibo.com"},
        ], original)
        self.assertEqual(candidate, {"SUB": "new", "SUBP": "new-p"})

    def test_mobile_netscape_cookies_are_not_used_as_desktop(self):
        raw = "# Netscape HTTP Cookie File\n.weibo.cn\tTRUE\t/\tTRUE\t0\tSUB\tmobile"
        self.assertEqual(WeiboExtractor._parse_cookie_header(raw), {})

    def test_configuration_does_not_reset_refresh_interval(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old")
        extractor._last_cookie_refresh_at = 123
        extractor.set_cookie("SUB=old")
        extractor.configure_cookie_refresh(True, 12)
        self.assertEqual(extractor._last_cookie_refresh_at, 123)

    async def test_failed_cookie_refresh_candidates_are_not_saved(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old")
        response = httpx.Response(200, headers={"Set-Cookie": "SUB=new; Domain=.weibo.com"},
                                  request=httpx.Request("GET", "https://weibo.com/", headers={"Cookie": extractor.cookie}))
        from data.plugins.astrbot_plugin_link_resolver.core.weibo import WeiboAuthError
        for error in ("logged out", "failed request"):
            extractor._refresh_cookie_candidate = AsyncMock(side_effect=WeiboAuthError(error))
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "cookie.txt"
                callbacks = []
                extractor.set_cookie_storage(target, callbacks.append)
                await extractor._capture_cookie_updates(response)
                self.assertEqual(extractor._user_cookies, {"SUB": "old"})
                self.assertFalse(target.exists())
                self.assertEqual(callbacks, [])

    async def test_manual_replacement_during_validation_wins(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old")
        original = dict(extractor._user_cookies)
        generation = extractor._cookie_generation
        extractor.set_cookie("SUB=manual")
        extractor._commit_cookie_candidate(original, {"SUB": "new"}, generation)
        self.assertEqual(extractor._user_cookies, {"SUB": "manual"})

    async def test_refresh_saves_server_rotation_without_requiring_uid(self):
        extractor = WeiboExtractor()
        requests = []
        def respond(request):
            self.assertEqual(str(request.url), "https://weibo.com/ajax/config/get_config")
            requests.append(request.headers["Cookie"])
            headers = {"Set-Cookie": "SUB=new; Domain=.weibo.com"} if len(requests) == 1 else {"Set-Cookie": "XSRF-TOKEN=rotating; Domain=.weibo.com"}
            return httpx.Response(200, json={"ok": 1, "data": {}}, headers=headers)
        real_client = httpx.AsyncClient
        with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                   side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            verified = await extractor._refresh_cookie_candidate({"SUB": "old"})
        self.assertEqual(verified, {"SUB": "new"})
        self.assertEqual(requests, ["SUB=old"])

    async def test_refresh_rejects_logout_and_deleted_credentials(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo import WeiboAuthError
        extractor = WeiboExtractor()
        real_client = httpx.AsyncClient
        for payload, headers in [
            ({"ok": 1, "data": {"login": False}}, {}),
            ({"ok": 1, "data": {"uid": "123"}}, {"Set-Cookie": "SUB=; Max-Age=0; Domain=.weibo.com"}),
        ]:
            with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                       side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload, headers=headers)), **kwargs)):
                with self.assertRaises(WeiboAuthError):
                    await extractor._refresh_cookie_candidate({"SUB": "old"})

    async def test_cookie_refresh_does_not_require_or_compare_uid(self):
        extractor = WeiboExtractor()
        real_client = httpx.AsyncClient
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        for data in ({}, {"uid": None}, {"uid": 0}, {"uid": "0"}, {"uid": "456"}):
            with self.subTest(data=data), patch(module + ".httpx.AsyncClient",
                    side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json={"ok": 1, "data": data},
                            headers={"Set-Cookie": "SUB=renewed; Domain=.weibo.com"})), **kwargs)):
                refreshed = await extractor._refresh_cookie_candidate({"SUB": "cookie"})
                self.assertEqual(refreshed, {"SUB": "renewed"})

    async def test_cookie_refresh_rejects_unsuccessful_responses(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo import WeiboAuthError
        extractor = WeiboExtractor()
        real_client = httpx.AsyncClient
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        for payload in ({"ok": 0, "data": {}}, {"ok": 1}, {"ok": 1, "data": {"login": False}}):
            with self.subTest(payload=payload), patch(module + ".httpx.AsyncClient",
                    side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=payload)), **kwargs)):
                with self.assertRaises(WeiboAuthError):
                    await extractor._refresh_cookie_candidate({"SUB": "cookie"})

    async def test_login_endpoint_404_does_not_report_cookie_expiration(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo import WeiboAuthError
        extractor = WeiboExtractor()
        real_client = httpx.AsyncClient
        with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(404)), **kwargs)):
            with self.assertRaisesRegex(WeiboAuthError, "无法判断 Cookie 是否有效"):
                await extractor._refresh_cookie_candidate({"SUB": "cookie"})

    async def test_keepalive_fallback_saves_login_rotation(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old")
        extractor._refresh_cookie_candidate = AsyncMock(return_value={"SUB": "renewed"})
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            extractor.set_cookie_storage(target)
            with patch(module + ".collect_browser_cookies", new=AsyncMock(side_effect=RuntimeError("unavailable"))) as browser:
                await extractor._refresh_user_cookies()
            self.assertEqual(browser.call_args.kwargs["cookies"], {"SUB": "old"})
            self.assertEqual(browser.call_args.kwargs["refresh_urls"], ("https://weibo.com/",))
            self.assertEqual(target.read_text(encoding="utf-8"), "SUB=renewed\n")
        await extractor._refresh_user_cookies()
        self.assertEqual(extractor._refresh_cookie_candidate.await_count, 1)

    async def test_browser_keepalive_saves_updates_without_config_api(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=old")
        extractor._refresh_cookie_candidate = AsyncMock(side_effect=AssertionError("config API must not gate browser keepalive"))
        cookies = [{"name": "SUB", "value": "new", "domain": ".weibo.com", "path": "/"}]
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            extractor.set_cookie_storage(target)
            with patch(module + ".collect_browser_cookies", new=AsyncMock(return_value=cookies)) as browser:
                await extractor._refresh_user_cookies()
            self.assertEqual(target.read_text(encoding="utf-8"), "SUB=new\n")
            self.assertEqual(browser.call_args.kwargs["state_path"], target.with_suffix(".browser.json"))
            extractor._refresh_cookie_candidate.assert_not_awaited()

    async def test_srf_restore_uses_fresh_alt_and_collects_redirect_cookie(self):
        extractor = WeiboExtractor()
        original = {"SUB": "old", "SRF": "recovery-session"}
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.path == "/visitor/visitor":
                self.assertIn("SRF=recovery-session", request.headers["Cookie"])
                self.assertEqual(request.url.params["a"], "restore")
                return httpx.Response(200, text='restore_back({"retcode":20000000,"data":{"alt":"fresh-ticket"}});')
            if request.url.path == "/sso/v2/login":
                self.assertEqual(request.url.params["alt"], "fresh-ticket")
                self.assertEqual(request.url.params["source"], "visitor_restore")
                return httpx.Response(302, headers={"Location": "https://weibo.com/", "Set-Cookie": "SUB=renewed; Domain=.weibo.com; Path=/; HttpOnly"})
            self.assertEqual(request.url.host, "weibo.com")
            self.assertIn("SUB=renewed", request.headers["Cookie"])
            return httpx.Response(403)
        real_client = httpx.AsyncClient
        with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            restored = await extractor._restore_cookie_candidate(original)
        self.assertEqual(restored, {"SUB": "renewed", "SRF": "recovery-session"})
        self.assertEqual(len(requests), 3)
        self.assertEqual(original["SUB"], "old")

    async def test_restore_without_srf_or_alt_does_not_generate_guest_cookie(self):
        extractor = WeiboExtractor()
        module = "data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor"
        with patch(module + ".httpx.AsyncClient") as client:
            self.assertIsNone(await extractor._restore_cookie_candidate({"SUB": "old"}))
            client.assert_not_called()
        real_client = httpx.AsyncClient
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, text='restore_back({"retcode":20000000,"data":{}});')
        with patch(module + ".httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            self.assertIsNone(await extractor._restore_cookie_candidate({"SUB": "old", "SRF": "session"}))
        self.assertEqual(len(requests), 1)

    async def test_restore_rejects_deletion_and_unrelated_redirects(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo import WeiboAuthError
        extractor = WeiboExtractor()
        real_client = httpx.AsyncClient
        for headers in (
            {"Set-Cookie": "SUB=; Max-Age=0; Domain=.weibo.com; Path=/"},
            {"Location": "https://example.com/", "Set-Cookie": "SUB=new; Domain=.weibo.com; Path=/"},
        ):
            def respond(request):
                if request.url.path == "/visitor/visitor":
                    return httpx.Response(200, text='restore_back({"retcode":20000000,"data":{"alt":"fresh-ticket"}});')
                return httpx.Response(302 if "Location" in headers else 200, headers=headers)
            with patch("data.plugins.astrbot_plugin_link_resolver.core.weibo.extractor.httpx.AsyncClient",
                    side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
                with self.assertRaises(WeiboAuthError):
                    await extractor._restore_cookie_candidate({"SUB": "old", "SRF": "session"})

    async def test_background_loop_runs_without_parsing_and_cancels(self):
        import asyncio
        extractor = WeiboExtractor()
        started = asyncio.Event()
        async def refresh():
            started.set()
        extractor._refresh_user_cookies = AsyncMock(side_effect=refresh)
        task = asyncio.create_task(extractor.cookie_keepalive_loop())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        extractor._refresh_user_cookies.assert_awaited_once()

    def test_desktop_response_rejects_mobile_sibling_domains_and_paths(self):
        response = httpx.Response(200, headers=[
            ("Set-Cookie", "SUB=mobile; Domain=.weibo.cn; Path=/"),
            ("Set-Cookie", "SUB=sibling; Domain=passport.weibo.com; Path=/"),
            ("Set-Cookie", "SUB=scoped; Domain=.weibo.com; Path=/other"),
            ("Set-Cookie", "XSRF-TOKEN=ok; Domain=.weibo.com; Path=/"),
        ], request=httpx.Request("GET", "https://weibo.com/"))
        self.assertEqual(WeiboExtractor._desktop_cookie_updates(response), {"XSRF-TOKEN": "ok"})

    async def test_plugin_lifecycle_starts_one_keepalive_task_and_stops_it(self):
        import ast
        import asyncio
        from types import SimpleNamespace
        source = Path(__file__).resolve().parents[1] / "main.py"
        plugin = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                      if isinstance(node, ast.ClassDef) and node.name == "LinkResolverPlugin")
        methods = [node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name in {"initialize", "terminate"}]
        namespace = {"asyncio": asyncio}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        started = asyncio.Event()
        async def keepalive():
            started.set()
            await asyncio.Event().wait()
        plugin = SimpleNamespace(_weibo_keepalive_task=None, font_auto_install_enabled=False,
                                 weibo_extractor=SimpleNamespace(cookie_keepalive_loop=keepalive))
        await namespace["initialize"](plugin)
        task = plugin._weibo_keepalive_task
        await asyncio.wait_for(started.wait(), timeout=1)
        await namespace["initialize"](plugin)
        self.assertIs(plugin._weibo_keepalive_task, task)
        await namespace["terminate"](plugin)
        self.assertTrue(task.cancelled())
        self.assertIsNone(plugin._weibo_keepalive_task)

    def test_new_configured_cookie_replaces_legacy_file(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo.cookie_storage import load_cookie_source
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            target.write_text("SUB=expired", encoding="utf-8")
            self.assertEqual(load_cookie_source("SUB=fresh", target), "SUB=fresh")
            self.assertEqual(target.read_text(encoding="utf-8").strip(), "SUB=fresh")

    def test_renewal_survives_reload_with_unchanged_configuration(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo.cookie_storage import load_cookie_source, record_refreshed_source
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            load_cookie_source("SUB=original", target)
            target.write_text("SUB=renewed", encoding="utf-8")
            record_refreshed_source("SUB=renewed", target)
            self.assertEqual(load_cookie_source("SUB=original", target), "SUB=renewed")
            self.assertEqual(load_cookie_source("SUB=renewed", target), "SUB=renewed")
            target.write_text("SUB=renewed-again", encoding="utf-8")
            record_refreshed_source("SUB=renewed-again", target)
            self.assertEqual(load_cookie_source("SUB=original", target), "SUB=renewed-again")

    def test_manual_cookie_replacement_after_renewal_wins(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo.cookie_storage import load_cookie_source, record_refreshed_source
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            load_cookie_source("SUB=original", target)
            target.write_text("SUB=renewed", encoding="utf-8")
            record_refreshed_source("SUB=renewed", target)
            self.assertEqual(load_cookie_source("SUB=manual", target), "SUB=manual")
            target.write_text("SUB=manual-renewed", encoding="utf-8")
            record_refreshed_source("SUB=manual-renewed", target)
            self.assertEqual(load_cookie_source("SUB=manual", target), "SUB=manual-renewed")

    def test_empty_configuration_reads_cookie_file(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo.cookie_storage import load_cookie_source
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            self.assertEqual(load_cookie_source("", target), "")
            target.write_text("SUB=file", encoding="utf-8")
            self.assertEqual(load_cookie_source("", target), "SUB=file")

    def test_corrupt_source_marker_does_not_ignore_new_cookie(self):
        from data.plugins.astrbot_plugin_link_resolver.core.weibo.cookie_storage import load_cookie_source
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cookie.txt"
            target.write_text("SUB=expired", encoding="utf-8")
            target.with_name("cookie.txt.source.json").write_text("[]", encoding="utf-8")
            self.assertEqual(load_cookie_source("SUB=fresh", target), "SUB=fresh")
            marker = target.with_name("cookie.txt.source.json").read_text(encoding="utf-8")
            self.assertNotIn("fresh", marker)
            self.assertNotIn("expired", marker)

    def test_extract_status_payload_accepts_wrapped_data_and_idstr(self):
        payload = {
            "ok": 1,
            "data": {
                "idstr": "5326658044953378",
                "bid": "PzAbCdEfG",
                "text_raw": "包在 data 里的微博",
                "pics": [
                    {"large": {"url": "https://wx4.sinaimg.cn/large/pic1.jpg"}}
                ],
            },
        }

        status = WeiboExtractor._extract_status_payload(payload)
        result = WeiboExtractor()._build_result(
            status, "https://weibo.com/6894541817/5326658044953378"
        )

        self.assertEqual(status["text_raw"], "包在 data 里的微博")
        self.assertEqual(result.weibo_id, "PzAbCdEfG")
        self.assertEqual(result.image_urls, ["https://wx4.sinaimg.cn/large/pic1.jpg"])

    def test_build_result_prefers_long_text_and_original_image(self):
        extractor = WeiboExtractor(download_original=True)
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "博主甲"},
            "isLongText": True,
            "longTextContent_raw": "完整正文\\n第二行",
            "text_raw": "截断正文",
            "pic_ids": ["pic1"],
            "pic_infos": {
                "pic1": {
                    "largest": {"url": "https://wx4.sinaimg.cn/large/pic1.jpg"},
                    "large": {"url": "https://wx4.sinaimg.cn/orj960/pic1.jpg"},
                }
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertEqual(result.text, "完整正文\\n第二行")
        self.assertEqual(result.image_urls, ["https://wx4.sinaimg.cn/large/pic1.jpg"])
        self.assertIsNone(result.video_url)

    async def test_hydrates_missing_long_text(self):
        extractor = WeiboExtractor()
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "user": {"screen_name": "博主甲"},
            "isLongText": True,
            "text_raw": "这是短正文...展开全文",
            "pic_ids": ["pic1"],
            "pic_infos": {
                "pic1": {"large": {"url": "https://wx4.sinaimg.cn/orj960/pic1.jpg"}}
            },
        }
        extractor._fetch_long_text_json = AsyncMock(
            return_value={"longTextContent": "这是完整正文，后半段不会丢。"}
        )

        await extractor._hydrate_long_texts(status, {"SUB": "foo"})
        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        extractor._fetch_long_text_json.assert_awaited_once_with(
            "AbCdEfGhI", {"SUB": "foo"}
        )
        self.assertEqual(result.text, "这是完整正文，后半段不会丢。")

    def test_build_result_picks_highest_bitrate_video(self):
        extractor = WeiboExtractor()
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "博主乙"},
            "text_raw": "视频微博",
            "page_info": {
                "type": "video",
                "page_pic": {"url": "https://wx4.sinaimg.cn/large/cover.jpg"},
                "media_info": {
                    "playback_list": [
                        {
                            "play_info": {
                                "bitrate": 1200,
                                "url": "https://media.example.com/low.mp4",
                            }
                        },
                        {
                            "play_info": {
                                "bitrate": 4800,
                                "url": "https://media.example.com/high.mp4",
                            }
                        },
                    ]
                },
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertEqual(result.video_url, "https://media.example.com/high.mp4")
        self.assertEqual(result.cover_url, "https://wx4.sinaimg.cn/large/cover.jpg")
        self.assertEqual(result.image_urls, [])

    def test_build_result_supports_mix_media_info(self):
        extractor = WeiboExtractor()
        status = {
            "id": "5327398318903668",
            "mblogid": "RbuKPBmfy",
            "text_raw": "新版混合媒体微博",
            "mix_media_info": {
                "items": [
                    {
                        "type": "video",
                        "data": {
                            "page_pic": {
                                "url": "https://wx3.sinaimg.cn/orj480/cover.jpg"
                            },
                            "media_info": {
                                "playback_list": [
                                    {
                                        "play_info": {
                                            "bitrate": 1200,
                                            "url": "https://media.example.com/low.mp4",
                                        }
                                    },
                                    {
                                        "play_info": {
                                            "bitrate": 4800,
                                            "url": "https://media.example.com/high.mp4",
                                        }
                                    },
                                ]
                            },
                        },
                    },
                    {
                        "type": "pic",
                        "data": {
                            "original": {
                                "url": "https://wx2.sinaimg.cn/orj1080/pic.jpg"
                            },
                            "largest": {
                                "url": "https://wx2.sinaimg.cn/large/pic.jpg"
                            },
                        },
                    },
                ]
            },
        }

        self.assertTrue(WeiboExtractor._status_has_media(status))
        result = extractor._build_result(
            status, "https://weibo.com/6036567968/5327398318903668"
        )

        self.assertEqual(result.video_url, "https://media.example.com/high.mp4")
        self.assertEqual(result.cover_url, "https://wx3.sinaimg.cn/orj480/cover.jpg")
        self.assertEqual(
            result.image_urls, ["https://wx2.sinaimg.cn/large/pic.jpg"]
        )

    def test_build_result_falls_back_to_retweeted_status_media(self):
        extractor = WeiboExtractor()
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "转发者"},
            "text_raw": "转发评论",
            "retweeted_status": {
                "text_raw": "原微博正文",
                "user": {"screen_name": "原作者"},
                "pic_ids": ["pic1"],
                "pic_infos": {
                    "pic1": {
                        "large": {"url": "https://wx4.sinaimg.cn/orj960/original.jpg"}
                    }
                },
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertIn("转发评论", result.text)
        self.assertIn("转发自 @原作者", result.text)
        self.assertEqual(
            result.image_urls, ["https://wx4.sinaimg.cn/orj960/original.jpg"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
