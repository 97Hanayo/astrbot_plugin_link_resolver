# ruff: noqa: E402
"""Tests for Xiaohongshu threshold relay-and-merge and 20-node forward chunking."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video
from data.plugins.astrbot_plugin_link_resolver.core.common.qq_video_relay import (
    RelayVideoReferenceNode,
    send_components_to_private_relay,
)
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu import XiaohongshuResult
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler import (
    XHS_MERGE_FORWARD_MAX_NODES,
    XiaohongshuMixin,
)
from data.plugins.astrbot_plugin_link_resolver.main import LinkResolver


class DummyEvent:
    def __init__(self, *, platform_id: str = "qq", umo: str = "qq:GroupMessage:999"):
        self._platform_id = platform_id
        self.unified_msg_origin = umo
        self.sent_actions: list[tuple[str, dict]] = []
        self._next_msg_id = 1000
        self.bot = SimpleNamespace(call_action=self._call_action)

    async def _call_action(self, action: str, **kwargs):
        self.sent_actions.append((action, kwargs))
        self._next_msg_id += 1
        return {"message_id": self._next_msg_id}

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_self_id(self) -> str:
        return "888888"

    def chain_result(self, chain):
        return chain


def _make_xhs_plugin(
    *,
    image_paths: list[Path],
    comment_paths: list[Path] | None = None,
    live_video_paths: list[Path] | None = None,
    threshold_mb: int = 1,
    relay_umo: str = "qq:FriendMessage:123456",
    render_card: bool = False,
    card_path: Path | None = None,
    qq_image_size_limit_mb: int = 0,
):
    image_urls = [f"https://example.com/img_{i}.jpg" for i in range(len(image_paths))]
    live_urls = (
        [f"https://example.com/live_{i}.mp4" for i in range(len(live_video_paths))]
        if live_video_paths
        else []
    )

    dl_image_iter = iter(image_paths)
    dl_video_iter = iter(live_video_paths or [])

    plugin = SimpleNamespace(
        xhs_enabled=True,
        xhs_summary_mode="渲染卡片" if render_card else "文字摘要",
        xhs_render_card=render_card,
        xhs_merge_send=False,
        xhs_max_media=99,
        xhs_concurrent_download=False,
        xhs_auto_unmerge_threshold_mb=threshold_mb,
        xhs_qq_image_size_limit_mb=qq_image_size_limit_mb,
        xhs_enable_live_photo=bool(live_video_paths),
        xhs_live_photo_video_limit=0,
        xhs_live_photo_format="视频",
        xhs_enable_comment_screenshot=bool(comment_paths),
        video_relay_umo=relay_umo,
        retry_count=0,
        max_video_size_mb=200,
        xhs_extractor=SimpleNamespace(
            parse=AsyncMock(
                return_value=XiaohongshuResult(
                    title="小红书测试标题",
                    author="测试作者",
                    text="测试正文",
                    image_urls=image_urls,
                    file_ids=[],
                    video_url=None,
                    cover_url=None,
                    source_url="https://www.xiaohongshu.com/explore/abc123",
                    note_id="abc123",
                    live_photo_urls=live_urls,
                    live_photo_url_candidates=[[u] for u in live_urls],
                    live_photo_video_urls=live_urls,
                )
            )
        ),
        _refresh_config=lambda: None,
        _send_reaction_emoji=AsyncMock(),
        _download_xhs_image=AsyncMock(side_effect=lambda *_a, **_kw: next(dl_image_iter)),
        _download_xhs_video=AsyncMock(side_effect=lambda *_a, **_kw: next(dl_video_iter)),
        _render_xhs_card=AsyncMock(return_value=card_path),
        _capture_xhs_comment_screenshots=AsyncMock(return_value=comment_paths or []),
        _prepare_component_for_merge_send=AsyncMock(
            side_effect=lambda component: component
        ),
        _get_merge_sender_uin=lambda event: "10001",
        _relay_video_reference_nodes=AsyncMock(return_value=None),
        cleanup_files=AsyncMock(),
    )
    plugin._build_xhs_summary = XiaohongshuMixin._build_xhs_summary.__get__(
        plugin, XiaohongshuMixin
    )
    plugin._relay_component_reference_nodes = (
        LinkResolver._relay_component_reference_nodes.__get__(plugin, LinkResolver)
    )
    return plugin


class TestRelayComponents(unittest.IsolatedAsyncioTestCase):
    async def test_send_components_to_private_relay_preserves_order_and_types(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "a.jpg"
            img_path.write_bytes(b"img-bytes")
            file_path = Path(tmpdir) / "b.png"
            file_path.write_bytes(b"file-bytes")
            vid_path = Path(tmpdir) / "c.mp4"
            vid_path.write_bytes(b"vid-bytes")

            components = [
                Plain("摘要文本"),
                Image.fromFileSystem(str(img_path)),
                File(file=str(file_path), name="b.png"),
                Video.fromFileSystem(str(vid_path)),
            ]

            refs = await send_components_to_private_relay(
                event, "qq:FriendMessage:123456", components
            )

        self.assertEqual(len(refs), 4)
        self.assertTrue(all(isinstance(r, RelayVideoReferenceNode) for r in refs))
        self.assertEqual([r.id for r in refs], [1001, 1002, 1003, 1004])
        self.assertEqual(len(event.sent_actions), 4)
        sent_types = [
            kwargs["message"][0]["type"] for _, kwargs in event.sent_actions
        ]
        self.assertEqual(sent_types, ["text", "image", "file", "video"])
        self.assertEqual(
            event.sent_actions[0][1]["message"][0]["data"]["text"], "摘要文本"
        )
        self.assertEqual(event.sent_actions[0][1]["user_id"], 123456)
        self.assertEqual(
            [r.toDict() for r in refs],
            [
                {"type": "node", "data": {"id": 1001}},
                {"type": "node", "data": {"id": 1002}},
                {"type": "node", "data": {"id": 1003}},
                {"type": "node", "data": {"id": 1004}},
            ],
        )


class TestXhsRelayMerge(unittest.IsolatedAsyncioTestCase):
    def test_max_nodes_constant_is_20(self):
        self.assertEqual(XHS_MERGE_FORWARD_MAX_NODES, 20)

    async def test_exceed_threshold_relays_in_strict_order_and_splits_by_20(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            comment_paths = [tmp / "comment_0.png", tmp / "comment_1.png"]
            for p in comment_paths:
                p.write_bytes(b"c" * 100)

            image_paths = [tmp / f"img_{i:02d}.jpg" for i in range(40)]
            for idx, p in enumerate(image_paths):
                # Total > 1MB to trigger threshold_mb=1
                p.write_bytes(b"i" * (30 * 1024) if idx == 0 else b"i" * (30 * 1024))

            live_paths = [tmp / "live_0.mp4", tmp / "live_1.mp4"]
            for p in live_paths:
                p.write_bytes(b"v" * 100)

            plugin = _make_xhs_plugin(
                image_paths=image_paths,
                comment_paths=comment_paths,
                live_video_paths=live_paths,
                threshold_mb=1,
                relay_umo="qq:FriendMessage:123456",
            )

            with patch(
                "data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep_mock:
                results = [
                    r
                    async for r in XiaohongshuMixin._process_xhs(
                        plugin, event, "https://www.xiaohongshu.com/explore/abc123"
                    )
                ]

        # 1 summary + 2 comments + 40 images + 2 live videos = 45 items -> [20, 20, 5]
        self.assertEqual(len(results), 3)
        self.assertEqual(sleep_mock.await_count, 2)
        chunk_sizes = [len(r[0].nodes) for r in results]
        self.assertEqual(chunk_sizes, [20, 20, 5])
        self.assertTrue(all(isinstance(r[0], Nodes) for r in results))

        # Verify relay private messages sent in strict order:
        # 0: Plain(summary), 1..2: comments, 3..42: images, 43..44: live videos
        self.assertEqual(len(event.sent_actions), 45)
        sent_messages = [kwargs["message"][0] for _, kwargs in event.sent_actions]
        self.assertEqual(sent_messages[0]["type"], "text")
        self.assertIn("小红书测试标题", sent_messages[0]["data"]["text"])
        self.assertEqual(sent_messages[1]["type"], "image")
        self.assertEqual(sent_messages[2]["type"], "image")
        for idx in range(3, 43):
            self.assertEqual(sent_messages[idx]["type"], "image")
        self.assertEqual(sent_messages[43]["type"], "video")
        self.assertEqual(sent_messages[44]["type"], "video")

        # Verify forwarded node IDs match sent message IDs in exact order (1001..1045)
        forwarded_ids = [
            node.id for chunk in results for node in chunk[0].nodes
        ]
        self.assertEqual(forwarded_ids, list(range(1001, 1046)))
        # Ensure standalone videos were NOT sent separately again
        self.assertFalse(any(isinstance(r[0], Video) for r in results))

    async def test_exceed_threshold_exact_20_and_21_boundary(self):
        for image_count, expected_chunks in [(19, [20]), (20, [20, 1])]:
            with self.subTest(image_count=image_count):
                event = DummyEvent()
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp = Path(tmpdir)
                    image_paths = [tmp / f"img_{i:02d}.jpg" for i in range(image_count)]
                    for p in image_paths:
                        p.write_bytes(b"x" * (128 * 1024))  # >= 2.3MB total > 1MB

                    plugin = _make_xhs_plugin(
                        image_paths=image_paths,
                        threshold_mb=1,
                        relay_umo="qq:FriendMessage:123456",
                    )
                    with patch(
                        "data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler.asyncio.sleep",
                        new_callable=AsyncMock,
                    ):
                        results = [
                            r
                            async for r in XiaohongshuMixin._process_xhs(
                                plugin,
                                event,
                                "https://www.xiaohongshu.com/explore/abc123",
                            )
                        ]

                self.assertEqual([len(r[0].nodes) for r in results], expected_chunks)
                forwarded_ids = [node.id for chunk in results for node in chunk[0].nodes]
                self.assertEqual(
                    forwarded_ids, list(range(1001, 1001 + image_count + 1))
                )

    async def test_exceed_threshold_falls_back_to_unmerge_when_relay_fails(self):
        event = DummyEvent()
        event.bot.call_action = AsyncMock(side_effect=RuntimeError("relay error"))
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            image_paths = [tmp / "a.jpg", tmp / "b.jpg"]
            for p in image_paths:
                p.write_bytes(b"x" * (1024 * 1024))  # 2MB > 1MB

            plugin = _make_xhs_plugin(
                image_paths=image_paths,
                threshold_mb=1,
                relay_umo="qq:FriendMessage:123456",
            )
            with patch(
                "data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                results = [
                    r
                    async for r in XiaohongshuMixin._process_xhs(
                        plugin, event, "https://www.xiaohongshu.com/explore/abc123"
                    )
                ]

        self.assertEqual(len(results), 3)
        self.assertIsInstance(results[0][0], Plain)
        self.assertIsInstance(results[1][0], Image)
        self.assertIsInstance(results[2][0], Image)

    async def test_below_threshold_merges_directly_without_relay_and_splits_by_20(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            image_paths = [tmp / f"img_{i:02d}.jpg" for i in range(25)]
            for p in image_paths:
                p.write_bytes(b"small")

            plugin = _make_xhs_plugin(
                image_paths=image_paths,
                threshold_mb=50,
                relay_umo="qq:FriendMessage:123456",
            )
            with patch(
                "data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                results = [
                    r
                    async for r in XiaohongshuMixin._process_xhs(
                        plugin, event, "https://www.xiaohongshu.com/explore/abc123"
                    )
                ]

        # Relay should NOT have been called when total size is below threshold
        self.assertEqual(event.sent_actions, [])
        # 1 summary + 25 images = 26 -> [20, 6]
        self.assertEqual([len(r[0].nodes) for r in results], [20, 6])
        all_nodes = [node for r in results for node in r[0].nodes]
        self.assertIsInstance(all_nodes[0].content[0], Plain)
        for idx, p in enumerate(image_paths, start=1):
            self.assertIsInstance(all_nodes[idx].content[0], Image)
            self.assertEqual(all_nodes[idx].content[0].path, str(p.resolve()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
