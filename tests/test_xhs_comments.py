# ruff: noqa: E402
"""Tests for Xiaohongshu comment screenshot helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PillowImage, ImageDraw

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from astrbot.api.message_components import Image, Node, Plain
from data.plugins.astrbot_plugin_link_resolver.core.common.font_manager import (
    ManagedFontPaths,
)
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu import (
    XiaohongshuResult,
)
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.comments import (
    COMMENT_MODE_DRAW,
    COMMENT_MODE_WEB,
    _normalize_xhs_url,
    _trim_comment_fragment,
    parse_xhs_cookies,
)
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler import (
    XiaohongshuMixin,
)
from data.plugins.astrbot_plugin_link_resolver.main import LinkResolver


class DummyEvent:
    def chain_result(self, chain):
        return chain


class TestXhsCommentCookies(unittest.TestCase):
    def test_parse_netscape_cookies_keeps_xhs_domain_only(self):
        raw = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".xiaohongshu.com\tTRUE\t/\tTRUE\t1893456000\ta1\tv1",
                ".example.com\tTRUE\t/\tTRUE\t1893456000\ta2\tv2",
                "www.xiaohongshu.com\tFALSE\t/\tFALSE\t0\tweb_session\tabc",
            ]
        )

        cookies = parse_xhs_cookies(raw)

        self.assertEqual([cookie["name"] for cookie in cookies], ["a1", "web_session"])
        self.assertEqual(cookies[0]["domain"], ".xiaohongshu.com")
        self.assertEqual(cookies[0]["expires"], 1893456000)
        self.assertTrue(cookies[0]["secure"])
        self.assertFalse(cookies[1]["secure"])

    def test_parse_cookie_header_uses_xhs_domain(self):
        cookies = parse_xhs_cookies("a1=v1; web_session=abc")

        self.assertEqual([cookie["name"] for cookie in cookies], ["a1", "web_session"])
        self.assertEqual(cookies[0]["domain"], ".xiaohongshu.com")
        self.assertEqual(cookies[0]["path"], "/")
        self.assertTrue(cookies[0]["secure"])

    def test_normalize_xhs_url_preserves_query_for_pc_note_access(self):
        source_url = (
            "https://www.xiaohongshu.com/explore/abc123"
            "?xsec_token=token&xsec_source=pc_feed#ignored"
        )

        url = _normalize_xhs_url(source_url, "abc123")

        self.assertEqual(
            url,
            "https://www.xiaohongshu.com/explore/abc123"
            "?xsec_token=token&xsec_source=pc_feed",
        )


class TestXhsCommentConfig(unittest.IsolatedAsyncioTestCase):
    def test_conf_schema_exposes_comment_screenshot_settings(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        items = schema["xhs_settings"]["items"]

        self.assertFalse(items["enable_comment_screenshot"]["default"])
        self.assertEqual(items["comment_screenshot_max"]["default"], 20)
        self.assertEqual(items["comment_screenshot_max"]["min"], 0)
        self.assertNotIn("comment_reply_screenshot_max", items)
        self.assertEqual(items["comment_screenshot_mode"]["default"], COMMENT_MODE_WEB)
        self.assertEqual(
            items["comment_screenshot_mode"]["options"],
            [COMMENT_MODE_WEB, COMMENT_MODE_DRAW],
        )
        self.assertEqual(items["cookies"]["default"], "")

    def test_refresh_config_defaults_comment_screenshot_settings(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {}
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with (
            patch.object(plugin, "_configure_managed_fonts", lambda: None),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.get_user_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.get_managed_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.find_default_font",
                return_value=None,
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.find_emoji_font",
                return_value=None,
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCardRenderer"
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCommentScreenshotter"
            ),
        ):
            LinkResolver._refresh_config(plugin)

        self.assertFalse(plugin.xhs_enable_comment_screenshot)
        self.assertEqual(plugin.xhs_comment_screenshot_max, 20)
        self.assertEqual(plugin.xhs_comment_screenshot_mode, COMMENT_MODE_WEB)
        self.assertEqual(plugin.xhs_cookies, "")

    def test_refresh_config_preserves_zero_comment_limits(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {
            "xhs_settings": {
                "comment_screenshot_max": 0,
            }
        }
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with (
            patch.object(plugin, "_configure_managed_fonts", lambda: None),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.get_user_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.get_managed_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.find_default_font",
                return_value=None,
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.find_emoji_font",
                return_value=None,
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCardRenderer"
            ),
            patch(
                "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCommentScreenshotter"
            ),
        ):
            LinkResolver._refresh_config(plugin)

        self.assertEqual(plugin.xhs_comment_screenshot_max, 0)

    def test_refresh_config_writes_xhs_cookies_to_file(self):
        plugin = LinkResolver.__new__(LinkResolver)
        raw_cookies = "# Netscape HTTP Cookie File .xiaohongshu.com TRUE / TRUE 0 a1 v1"
        plugin.config = {"xhs_settings": {"cookies": raw_cookies}}
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_file = Path(tmpdir) / "cookies" / "xhs_cookies.txt"
            with (
                patch.object(plugin, "_configure_managed_fonts", lambda: None),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_user_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=None),
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_managed_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=None),
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.find_default_font",
                    return_value=None,
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.find_emoji_font",
                    return_value=None,
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCardRenderer"
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCommentScreenshotter"
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_xhs_cookies_file",
                    return_value=cookies_file,
                ),
            ):
                LinkResolver._refresh_config(plugin)

            self.assertTrue(cookies_file.exists())
            self.assertIn("\n.xiaohongshu.com", cookies_file.read_text("utf-8"))

    def test_refresh_config_reads_xhs_cookies_from_file_when_config_empty(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {}
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_file = Path(tmpdir) / "cookies" / "xhs_cookies.txt"
            cookies_file.parent.mkdir(parents=True)
            cookies_file.write_text("a1=v1; web_session=abc", encoding="utf-8")
            with (
                patch.object(plugin, "_configure_managed_fonts", lambda: None),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_user_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=None),
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_managed_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=None),
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.find_default_font",
                    return_value=None,
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.find_emoji_font",
                    return_value=None,
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCardRenderer"
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.XiaohongshuCommentScreenshotter"
                ),
                patch(
                    "data.plugins.astrbot_plugin_link_resolver.main.get_xhs_cookies_file",
                    return_value=cookies_file,
                ),
            ):
                LinkResolver._refresh_config(plugin)

        self.assertEqual(plugin.xhs_cookies, "a1=v1; web_session=abc")

    async def test_capture_xhs_comment_screenshots_passes_comment_limit(self):
        screenshotter = SimpleNamespace(capture=AsyncMock(return_value=[]))
        plugin = SimpleNamespace(
            xhs_enable_comment_screenshot=True,
            xhs_comment_screenshotter=screenshotter,
            xhs_comment_screenshot_max=8,
            xhs_comment_screenshot_mode=COMMENT_MODE_WEB,
            xhs_cookies="a=b",
        )
        result = XiaohongshuResult(
            title="标题",
            author="作者",
            text="正文",
            image_urls=[],
            file_ids=[],
            video_url=None,
            cover_url=None,
            source_url="https://www.xiaohongshu.com/explore/abc123",
            note_id="abc123",
        )

        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler.get_xhs_comment_path",
            return_value=Path("comments"),
        ):
            await XiaohongshuMixin._capture_xhs_comment_screenshots(
                plugin, result, result.source_url, "req1"
            )

        screenshotter.capture.assert_awaited_once()
        kwargs = screenshotter.capture.await_args.kwargs
        self.assertEqual(kwargs["max_comments"], 8)
        self.assertNotIn("max_replies_per_comment", kwargs)

    def test_trim_comment_fragment_removes_top_and_bottom_blank_space(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "fragment.png"
            image = PillowImage.new("RGB", (200, 420), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 180, 180, 230), fill=(60, 60, 60))
            image.save(path)
            image.close()

            _trim_comment_fragment(path, padding=12)

            trimmed = PillowImage.open(path)
            try:
                self.assertEqual(trimmed.width, 200)
                self.assertLess(trimmed.height, 100)
                self.assertGreaterEqual(trimmed.height, 70)
            finally:
                trimmed.close()


class TestXhsCommentSendOrder(unittest.IsolatedAsyncioTestCase):
    async def test_comment_screenshot_is_between_summary_and_media_in_merge(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = Path(tmpdir) / "media.jpg"
            comment_path = Path(tmpdir) / "comment.png"
            media_path.write_bytes(b"media")
            comment_path.write_bytes(b"comment")
            plugin = _make_xhs_plugin(
                media_path=media_path,
                comment_path=comment_path,
                auto_unmerge_threshold_mb=50,
            )

            results = []
            async for result in XiaohongshuMixin._process_xhs(
                plugin, event, "https://www.xiaohongshu.com/explore/abc123"
            ):
                results.append(result)

        nodes = results[0][0]
        self.assertIsInstance(nodes.nodes[0], Node)
        self.assertIsInstance(nodes.nodes[0].content[0], Plain)
        self.assertIsInstance(nodes.nodes[1].content[0], Image)
        self.assertIn("comment.png", nodes.nodes[1].content[0].path)
        self.assertIsInstance(nodes.nodes[2].content[0], Image)
        self.assertIn("media.jpg", nodes.nodes[2].content[0].path)

    async def test_comment_screenshot_is_between_summary_and_media_when_unmerged(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = Path(tmpdir) / "media.jpg"
            comment_path = Path(tmpdir) / "comment.png"
            media_path.write_bytes(b"x" * 2 * 1024 * 1024)
            comment_path.write_bytes(b"comment")
            plugin = _make_xhs_plugin(
                media_path=media_path,
                comment_path=comment_path,
                auto_unmerge_threshold_mb=1,
            )

            results = []
            async for result in XiaohongshuMixin._process_xhs(
                plugin, event, "https://www.xiaohongshu.com/explore/abc123"
            ):
                results.append(result)

        self.assertIsInstance(results[0][0], Plain)
        self.assertIn("小红书标题", results[0][0].text)
        self.assertIsInstance(results[1][0], Image)
        self.assertIn("comment.png", results[1][0].path)
        self.assertIsInstance(results[2][0], Image)
        self.assertIn("media.jpg", results[2][0].path)

    async def test_comment_screenshot_failure_keeps_media_send(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = Path(tmpdir) / "media.jpg"
            media_path.write_bytes(b"media")
            plugin = _make_xhs_plugin(media_path=media_path, comment_path=None)
            plugin._capture_xhs_comment_screenshots = AsyncMock(
                side_effect=RuntimeError("browser missing")
            )

            results = []
            async for result in XiaohongshuMixin._process_xhs(
                plugin, event, "https://www.xiaohongshu.com/explore/abc123"
            ):
                results.append(result)

        nodes = results[0][0]
        self.assertIsInstance(nodes.nodes[0].content[0], Plain)
        self.assertIsInstance(nodes.nodes[1].content[0], Image)
        self.assertIn("media.jpg", nodes.nodes[1].content[0].path)


def _make_xhs_plugin(
    *,
    media_path: Path,
    comment_path: Path | None,
    auto_unmerge_threshold_mb: int = 50,
):
    plugin = SimpleNamespace(
        xhs_enabled=True,
        xhs_summary_mode="文字摘要",
        xhs_render_card=False,
        xhs_merge_send=False,
        xhs_max_media=99,
        xhs_concurrent_download=False,
        xhs_auto_unmerge_threshold_mb=auto_unmerge_threshold_mb,
        xhs_qq_image_size_limit_mb=0,
        xhs_enable_comment_screenshot=True,
        retry_count=0,
        max_video_size_mb=200,
        xhs_extractor=SimpleNamespace(
            parse=AsyncMock(
                return_value=XiaohongshuResult(
                    title="小红书标题",
                    author="作者乙",
                    text="完整正文内容",
                    image_urls=["https://example.com/xhs.jpg"],
                    file_ids=[],
                    video_url=None,
                    cover_url=None,
                    source_url="https://www.xiaohongshu.com/explore/abc123",
                    note_id="abc123",
                )
            )
        ),
        _refresh_config=lambda: None,
        _send_reaction_emoji=AsyncMock(),
        _download_xhs_image=AsyncMock(return_value=media_path),
        _download_xhs_video=AsyncMock(),
        _render_xhs_card=AsyncMock(),
        _prepare_component_for_merge_send=AsyncMock(side_effect=lambda component: component),
        _get_merge_sender_uin=lambda event: "10001",
        cleanup_files=AsyncMock(),
    )
    plugin._build_xhs_summary = XiaohongshuMixin._build_xhs_summary.__get__(
        plugin, XiaohongshuMixin
    )
    plugin._capture_xhs_comment_screenshots = AsyncMock(
        return_value=[comment_path] if comment_path else []
    )
    return plugin


if __name__ == "__main__":
    unittest.main(verbosity=2)
