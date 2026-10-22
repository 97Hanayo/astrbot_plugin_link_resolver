# ruff: noqa: E402
"""Focused tests for Douyin cookie parsing."""

from __future__ import annotations

import sys
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

from data.plugins.astrbot_plugin_link_resolver.core.douyin import (
    DouyinExtractor,
    DouyinParseError,
)


class TestDouyinCookieParsing(unittest.TestCase):
    def test_parse_netscape_cookies_txt(self):
        raw = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".douyin.com\tTRUE\t/\tTRUE\t0\tpassport_csrf_token\tdouyin-token",
                "#HttpOnly_.iesdouyin.com\tTRUE\t/\tTRUE\t0\tmsToken\ties-token",
                ".example.com\tTRUE\t/\tTRUE\t0\tignored\tvalue",
            ]
        )

        self.assertEqual(
            DouyinExtractor._parse_cookie_header(raw),
            {"passport_csrf_token": "douyin-token", "msToken": "ies-token"},
        )

    def test_parse_cookie_header(self):
        raw = "Cookie: passport_csrf_token=douyin-token; msToken=ies-token"

        self.assertEqual(
            DouyinExtractor._parse_cookie_header(raw),
            {"passport_csrf_token": "douyin-token", "msToken": "ies-token"},
        )

    def test_set_cookie_updates_request_cookies(self):
        extractor = DouyinExtractor()
        extractor.set_cookie(".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tvalue")

        self.assertTrue(extractor.has_cookie())
        self.assertEqual(extractor.get_cookies(), {"ttwid": "value"})


class TestDouyinLivePhotos(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.extractor = DouyinExtractor()
        self.item = {
            "aweme_id": "123",
            "create_time": 0,
            "desc": "mixed gallery",
            "author": {"nickname": "author", "avatar_thumb": {"url_list": []}},
            "images": [
                {"url_list": ["https://example.com/static.jpg"]},
                {"url_list": [], "video": self.video("orphan")},
                {
                    "url_list": ["https://example.com/live.jpg"],
                    "video": self.video("live"),
                },
                {"url_list": ["https://example.com/last.jpg"]},
            ],
        }

    @staticmethod
    def video(name):
        return {
            "play_addr": {"url_list": [f"https://example.com/{name}-hevc.mp4"]},
            "play_addr_h264": {"url_list": [f"https://example.com/{name}.mp4"]},
            "cover": {"url_list": []},
            "duration": 1000,
        }

    async def test_share_page_preserves_live_image_alignment(self):
        import json

        router = {
            "loaderData": {
                "note_(id)/page": {"videoInfoRes": {"item_list": [self.item]}}
            }
        }
        response = httpx.Response(
            200, text=f"<script>window._ROUTER_DATA = {json.dumps(router)}</script>"
        )
        self.extractor._fetch_html = AsyncMock(return_value=response)
        result = await self.extractor.parse_video("https://example.com/share", "source")

        self.assertEqual(len(result.image_urls), 3)
        self.assertEqual(
            result.live_photo_video_urls, [None, "https://example.com/live.mp4", None]
        )
        self.assertEqual(result.dynamic_urls, ["https://example.com/live.mp4"])

    async def test_slides_requests_web_aid_and_keeps_all_70_live_photos(self):
        self.item["images"] = [
            {"url_list": [f"https://example.com/{i}.jpg"], "video": self.video(str(i))}
            for i in range(70)
        ]
        response = httpx.Response(
            200,
            json={"aweme_details": [self.item]},
            request=httpx.Request("GET", "https://example.com"),
        )
        client = AsyncMock()
        client.__aenter__.return_value.get.return_value = response
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.httpx.AsyncClient",
            return_value=client,
        ):
            result = await self.extractor.parse_slides("123", "source")

        params = client.__aenter__.return_value.get.call_args.kwargs["params"]
        self.assertEqual(params["aid"], "6383")
        self.assertEqual(len(result.image_urls), 70)
        self.assertEqual(len(result.live_photo_video_urls), 70)
        self.assertEqual(len(set(result.dynamic_urls)), 70)

    async def test_iteminfo_preserves_alignment_and_h264_preference(self):
        response = httpx.Response(
            200,
            json={"item_list": [self.item]},
            request=httpx.Request("GET", "https://example.com"),
        )
        client = AsyncMock()
        client.__aenter__.return_value.get.return_value = response
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.httpx.AsyncClient",
            return_value=client,
        ):
            result = await self.extractor.parse_iteminfo("123", "source")

        self.assertEqual(
            result.live_photo_video_urls, [None, "https://example.com/live.mp4", None]
        )

    async def test_share_page_regular_video_still_parses(self):
        import json

        self.item["images"] = []
        self.item["video"] = self.video("main")
        router = {
            "loaderData": {
                "video_(id)/page": {"videoInfoRes": {"item_list": [self.item]}}
            }
        }
        self.extractor._fetch_html = AsyncMock(
            return_value=httpx.Response(
                200, text=f"<script>window._ROUTER_DATA = {json.dumps(router)}</script>"
            )
        )
        result = await self.extractor.parse_video("https://example.com/share", "source")

        self.assertEqual(result.video_url, "https://example.com/main-hevc.mp4")
        self.assertEqual(result.live_photo_video_urls, [])

    async def test_note_link_uses_live_capable_slides_endpoint(self):
        self.extractor.parse_slides = AsyncMock(return_value="live gallery")
        result = await self.extractor.parse("https://www.douyin.com/note/123")
        self.assertEqual(result, "live gallery")
        self.extractor.parse_slides.assert_awaited_once_with(
            "123", "https://www.douyin.com/note/123"
        )

    async def test_note_link_falls_back_if_slides_unavailable(self):
        self.extractor.parse_slides = AsyncMock(
            side_effect=DouyinParseError("unavailable")
        )
        self.extractor.parse_video = AsyncMock(return_value="share gallery")
        result = await self.extractor.parse("https://www.douyin.com/note/123")
        self.assertEqual(result, "share gallery")

    async def test_invalid_slides_response_becomes_parse_error(self):
        client = AsyncMock()
        client.__aenter__.return_value.get.return_value = httpx.Response(
            200, content=b"", request=httpx.Request("GET", "https://example.com")
        )
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.httpx.AsyncClient",
            return_value=client,
        ):
            with self.assertRaises(DouyinParseError):
                await self.extractor.parse_slides("123", "source")


if __name__ == "__main__":
    unittest.main()
