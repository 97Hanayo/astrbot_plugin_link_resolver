# ruff: noqa: E402
"""Focused Live Photo download and delivery tests for Douyin."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break

from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from data.plugins.astrbot_plugin_link_resolver.core.common import SizeLimitExceeded
from data.plugins.astrbot_plugin_link_resolver.core.douyin import DouyinResult
from data.plugins.astrbot_plugin_link_resolver.core.douyin.handler import DouyinMixin


class TestDouyinLivePhotoHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result = DouyinResult(
            title="gallery",
            author="author",
            author_avatar=None,
            duration=0,
            video_url=None,
            cover_url=None,
            image_urls=["static", "live1", "live2"],
            dynamic_urls=["video1", "video2"],
            source_url="source",
            live_photo_video_urls=[None, "video1", "video2"],
        )
        self.plugin = SimpleNamespace(
            douyin_enabled=True,
            douyin_max_media=3,
            douyin_merge_send=False,
            douyin_enable_live_photo=True,
            douyin_live_photo_video_limit=1,
            douyin_live_photo_format="视频",
            max_video_size_mb=10,
            retry_count=0,
            video_relay_umo="relay",
            douyin_extractor=SimpleNamespace(parse=AsyncMock(return_value=self.result)),
            _refresh_config=lambda: None,
            _send_reaction_emoji=AsyncMock(),
            _get_merge_sender_uin=lambda event: "10001",
            _prepare_component_for_merge_send=AsyncMock(
                side_effect=lambda value: value
            ),
            _relay_video_reference_nodes=AsyncMock(return_value=[]),
            cleanup_files=AsyncMock(),
        )
        self.plugin._build_douyin_summary = DouyinMixin._build_douyin_summary.__get__(
            self.plugin
        )
        self.plugin._download_douyin_image = AsyncMock(side_effect=self.download_image)
        self.plugin._download_douyin_video = AsyncMock(side_effect=self.download_video)
        self.plugin._convert_douyin_live_photo = AsyncMock(side_effect=self.convert)
        self.event = SimpleNamespace(send=AsyncMock())

    async def download_image(self, url, request_id):
        path = self.root / f"{url}.jpg"
        path.write_bytes(b"image")
        return path

    async def download_video(self, url, request_id):
        path = self.root / f"{url}.mp4"
        path.write_bytes(b"video")
        return path

    async def convert(self, source, live_format):
        path = source.with_suffix(".gif" if live_format == "GIF" else ".converted.mp4")
        path.write_bytes(b"converted")
        return path

    def forwarded_components(self):
        nodes = self.event.send.call_args_list[0].args[0].chain[0]
        self.assertIsInstance(nodes, Nodes)
        return [component for node in nodes.nodes for component in node.content]

    async def test_static_limit_does_not_consume_live_quota_and_video_uses_relay(self):
        relay_node = Node(uin="10001", content=[Plain("relayed")])
        self.plugin._relay_video_reference_nodes.return_value = [relay_node]
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")

        self.assertEqual(self.plugin._download_douyin_image.await_count, 3)
        self.plugin._download_douyin_video.assert_awaited_once()
        self.assertEqual(self.plugin._download_douyin_video.call_args.args[0], "video1")
        videos = self.plugin._relay_video_reference_nodes.call_args.args[1]
        self.assertEqual(len(videos), 1)
        self.assertIsInstance(videos[0], Video)
        self.assertEqual(
            sum(isinstance(c, Image) for c in self.forwarded_components()), 3
        )
        self.assertEqual(self.event.send.await_count, 1)
        self.assertEqual(len(self.plugin.cleanup_files.call_args.args[0]), 5)

    async def test_disabled_live_sends_only_static_images(self):
        self.plugin.douyin_enable_live_photo = False
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.plugin._download_douyin_video.assert_not_awaited()
        self.assertEqual(
            sum(isinstance(c, Image) for c in self.forwarded_components()), 3
        )

    async def test_media_limit_excludes_live_videos_outside_selected_images(self):
        self.plugin.douyin_max_media = 1
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.plugin._download_douyin_video.assert_not_awaited()
        self.assertEqual(self.plugin._download_douyin_image.await_count, 1)

    async def test_converted_output_size_limit_preserves_static_images(self):
        self.plugin.max_video_size_mb = 0.000001
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.assertEqual(self.plugin._relay_video_reference_nodes.call_args.args[1], [])
        self.assertEqual(
            sum(isinstance(c, Image) for c in self.forwarded_components()), 3
        )

    async def test_regular_video_still_sends_without_live_conversion(self):
        self.result.image_urls = []
        self.result.dynamic_urls = []
        self.result.live_photo_video_urls = []
        self.result.video_url = "main_video"
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.plugin._convert_douyin_live_photo.assert_not_awaited()
        self.assertEqual(
            self.plugin._download_douyin_video.call_args.args[0], "main_video"
        )
        self.assertIsInstance(
            self.plugin._relay_video_reference_nodes.call_args.args[1][0], Video
        )

    async def test_unlimited_gifs_are_merged_and_sources_are_cleaned(self):
        self.plugin.douyin_live_photo_format = "GIF"
        self.plugin.douyin_live_photo_video_limit = 0
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.assertEqual(self.plugin._download_douyin_video.await_count, 2)
        self.assertEqual(
            sum(isinstance(c, Image) for c in self.forwarded_components()), 5
        )
        self.assertEqual(self.plugin._relay_video_reference_nodes.call_args.args[1], [])
        self.assertEqual(len(self.plugin.cleanup_files.call_args.args[0]), 7)

    async def test_failed_image_keeps_live_index_mapping(self):
        async def download(url, request_id):
            if url == "live1":
                raise RuntimeError("image failed")
            return await self.download_image(url, request_id)

        self.plugin._download_douyin_image.side_effect = download
        await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.assertEqual(self.plugin._download_douyin_video.call_args.args[0], "video2")

    async def test_conversion_and_size_failures_preserve_static_images(self):
        for failure in [RuntimeError("ffmpeg failed"), SizeLimitExceeded("too large")]:
            with self.subTest(failure=type(failure).__name__):
                self.plugin._convert_douyin_live_photo.side_effect = failure
                self.event.send.reset_mock()
                await DouyinMixin._process_douyin(self.plugin, self.event, "source")
                self.assertEqual(
                    sum(isinstance(c, Image) for c in self.forwarded_components()), 3
                )

    async def test_video_falls_back_to_standalone_send_without_relay(self):
        self.plugin.video_relay_umo = ""
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.handler.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        self.assertEqual(self.event.send.await_count, 2)
        self.assertIsInstance(self.event.send.call_args_list[1].args[0].chain[0], Video)

    async def test_70_images_and_70_relay_videos_split_into_default_chunks(self):
        self.result.image_urls = [f"image{i}" for i in range(70)]
        self.result.live_photo_video_urls = [f"video{i}" for i in range(70)]
        self.result.dynamic_urls = list(self.result.live_photo_video_urls)
        self.plugin.douyin_max_media = 99
        self.plugin.douyin_live_photo_video_limit = 0
        references = [
            Node(uin="10001", content=[Plain(f"relay{i}")]) for i in range(70)
        ]
        self.plugin._relay_video_reference_nodes.return_value = references

        async def send(chain):
            self.plugin.cleanup_files.assert_not_awaited()

        self.event.send.side_effect = send
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.handler.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await DouyinMixin._process_douyin(self.plugin, self.event, "source")

        chunks = [
            call.args[0].chain[0].nodes for call in self.event.send.call_args_list
        ]
        self.assertEqual([len(chunk) for chunk in chunks], [20] * 7 + [1])
        flattened = [node for chunk in chunks for node in chunk]
        self.assertIsInstance(flattened[0].content[0], Plain)
        self.assertEqual(
            sum(isinstance(node.content[0], Image) for node in flattened), 70
        )
        self.assertTrue(
            all(
                actual is expected
                for actual, expected in zip(flattened[-70:], references)
            )
        )
        self.assertEqual(sleep.await_count, 7)
        self.plugin.cleanup_files.assert_awaited_once()

    async def test_custom_chunk_size_and_exact_boundary(self):
        self.plugin.douyin_enable_live_photo = False
        for size, expected in [(0, [1] * 4), (2, [2, 2]), (4, [4]), (5, [4])]:
            with self.subTest(size=size):
                self.plugin.douyin_merge_forward_chunk_size = size
                self.event.send.reset_mock()
                with patch(
                    "data.plugins.astrbot_plugin_link_resolver.core.douyin.handler.asyncio.sleep",
                    new_callable=AsyncMock,
                ):
                    await DouyinMixin._process_douyin(self.plugin, self.event, "source")
                chunks = [
                    call.args[0].chain[0].nodes
                    for call in self.event.send.call_args_list
                ]
                self.assertEqual([len(chunk) for chunk in chunks], expected)

    async def test_oversized_chunk_setting_is_capped_at_100_including_gifs(self):
        self.result.image_urls = [f"image{i}" for i in range(70)]
        self.result.live_photo_video_urls = [f"video{i}" for i in range(70)]
        self.plugin.douyin_max_media = 99
        self.plugin.douyin_live_photo_video_limit = 0
        self.plugin.douyin_live_photo_format = "GIF"
        self.plugin.douyin_merge_forward_chunk_size = 200
        with patch(
            "data.plugins.astrbot_plugin_link_resolver.core.douyin.handler.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await DouyinMixin._process_douyin(self.plugin, self.event, "source")
        chunks = [
            call.args[0].chain[0].nodes for call in self.event.send.call_args_list
        ]
        self.assertEqual([len(chunk) for chunk in chunks], [100, 41])
        self.assertEqual(
            sum(
                isinstance(node.content[0], Image) for chunk in chunks for node in chunk
            ),
            140,
        )


if __name__ == "__main__":
    unittest.main()
