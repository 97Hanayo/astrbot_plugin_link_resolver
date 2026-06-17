# ruff: noqa: E402
"""B站 API 响应兼容性回归测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.bilibili.handler import (
    BilibiliMixin,
)


class TestBilibiliCompatibility(unittest.TestCase):
    """验证 bilibili-api-python 不同响应形态下的兼容逻辑。"""

    def test_none_current_quality_has_no_lower_quality_candidates(self):
        harness = BilibiliMixin.__new__(BilibiliMixin)
        harness.allow_hdr = False
        harness.allow_dolby = False

        self.assertEqual(
            harness._get_lower_qualities(SimpleNamespace(value=None)),
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
