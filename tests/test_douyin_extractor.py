# ruff: noqa: E402
"""Focused tests for Douyin cookie parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.douyin import DouyinExtractor


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


if __name__ == "__main__":
    unittest.main()
