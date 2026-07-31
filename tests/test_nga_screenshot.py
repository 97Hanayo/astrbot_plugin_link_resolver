# ruff: noqa: E402
from __future__ import annotations

import tempfile
import unittest
import dataclasses
import sys
from types import ModuleType
from pathlib import Path

from PIL import Image

_dataclass = dataclasses.dataclass
if sys.version_info < (3, 10):
    def _dataclass_without_slots(cls=None, /, **kwargs):
        kwargs.pop("slots", None)
        if cls is None:
            return lambda wrapped: _dataclass(wrapped, **kwargs)
        return _dataclass(cls, **kwargs)

    dataclasses.dataclass = _dataclass_without_slots

api_module = ModuleType("astrbot.api")
api_module.logger = __import__("logging").getLogger("test")
event_module = ModuleType("astrbot.api.event")
event_module.AstrMessageEvent = object
event_module.MessageChain = object
components_module = ModuleType("astrbot.api.message_components")
components_module.Image = object
components_module.Node = object
components_module.Nodes = object
components_module.Plain = object
sys.modules.setdefault("astrbot", ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", api_module)
sys.modules.setdefault("astrbot.api.event", event_module)
sys.modules.setdefault("astrbot.api.message_components", components_module)

from core.nga.screenshot import _fit_screenshot_limits, _sanitize_filename_part

dataclasses.dataclass = _dataclass


class NgaScreenshotLimitTests(unittest.TestCase):
    def test_sanitize_filename_part_keeps_safe_ascii(self):
        self.assertEqual(_sanitize_filename_part("NGA 主楼 / 热点回复"), "nga")
        self.assertEqual(_sanitize_filename_part("hot-reply_01"), "hot-reply_01")
        self.assertEqual(_sanitize_filename_part("   "), "section")

    def test_fit_screenshot_limits_caps_height(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tall.png"
            Image.new("RGB", (320, 12000), (246, 240, 223)).save(path, format="PNG")

            result = _fit_screenshot_limits(path)

            self.assertEqual(result, path)
            with Image.open(result) as image:
                self.assertEqual(image.height, 10000)
                self.assertLessEqual(image.width, 320)

    def test_fit_screenshot_limits_converts_oversized_png_under_five_mb(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large.png"
            image = Image.effect_noise((1200, 11000), 100).convert("RGB")
            image.save(path, format="PNG")
            image.close()

            result = _fit_screenshot_limits(path)

            self.assertEqual(result.suffix, ".jpg")
            self.assertFalse(path.exists())
            self.assertLessEqual(result.stat().st_size, 5 * 1024 * 1024)
            with Image.open(result) as image:
                self.assertLessEqual(image.height, 10000)


if __name__ == "__main__":
    unittest.main()
