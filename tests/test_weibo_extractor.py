# ruff: noqa: E402
"""Unit tests for the Weibo extractor.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_weibo_extractor.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

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
                "#HttpOnly_.weibo.cn\tTRUE\t/\tTRUE\t0\tSUBP\tweibo-subp",
                ".example.com\tTRUE\t/\tTRUE\t0\tignored\tvalue",
            ]
        )

        cookies = WeiboExtractor._parse_cookie_header(raw)

        self.assertEqual(cookies, {"SUB": "weibo-sub", "SUBP": "weibo-subp"})

    def test_parse_visitor_jsonp_cookies(self):
        payload = (
            'window.visitor_gray_callback && visitor_gray_callback({"retcode":20000000,'
            '"msg":"succ","data":{"sub":"visitor-sub","subp":"visitor-subp"}});'
        )

        cookies = WeiboExtractor._parse_visitor_jsonp_cookies(payload)

        self.assertEqual(cookies, {"SUB": "visitor-sub", "SUBP": "visitor-subp"})

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
