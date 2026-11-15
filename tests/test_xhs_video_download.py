"""Tests for Xiaohongshu video download integrity and recovery."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break

from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu import handler


class TestXhsVideoDownload(unittest.IsolatedAsyncioTestCase):
    content = b"0123456789"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "video.mp4"
        self.temp_path = self.output.with_suffix(".mp4.part")
        self.plugin = handler.XiaohongshuMixin()
        self.plugin.max_video_size_mb = 0
        self.requests = []

    def cdn_response(self, request):
        byte_range = request.headers.get("Range")
        self.requests.append(byte_range)
        if byte_range is None:
            self.assertFalse(self.temp_path.exists())
            return httpx.Response(200, content=self.content)
        start, end = map(int, byte_range.removeprefix("bytes=").split("-"))
        end = min(end, len(self.content) - 1)
        return httpx.Response(
            206,
            content=self.content[start : end + 1],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(self.content)}",
                "ETag": '"cdn-version"',
            },
        )

    async def download(self, respond):
        client = httpx.AsyncClient
        transport = httpx.MockTransport(respond)
        with (
            patch.object(
                handler.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: client(transport=transport, **kwargs),
            ),
            patch.object(handler, "XHS_VIDEO_DOWNLOAD_CHUNK_BYTES", 4),
            patch.object(self.plugin, "_build_xhs_path", return_value=self.output),
        ):
            return await self.plugin._download_xhs_video(
                "https://video.example/video.mp4",
                "test",
                expected_size=len(self.content),
            )

    async def test_matching_ranges_are_published_without_full_download(self):
        self.assertEqual(await self.download(self.cdn_response), self.output)
        self.assertEqual(self.output.read_bytes(), self.content)
        self.assertCountEqual(self.requests, ["bytes=0-3", "bytes=4-7", "bytes=8-9"])
        self.assertFalse(self.temp_path.exists())

    async def test_cdn_returning_full_body_for_range_uses_one_request(self):
        def respond(request):
            self.requests.append(request.headers.get("Range"))
            return httpx.Response(200, content=self.content)

        self.assertEqual(await self.download(respond), self.output)
        self.assertEqual(self.requests, ["bytes=0-3"])
        self.assertEqual(self.output.read_bytes(), self.content)

    async def test_missing_strong_etag_triggers_a_full_download(self):
        def respond(request):
            response = self.cdn_response(request)
            if request.headers.get("Range") is not None:
                response.headers["ETag"] = 'W/"cdn-version"'
            return response

        await self.download(respond)
        self.assertEqual(self.requests, ["bytes=0-3", None])
        self.assertEqual(self.output.read_bytes(), self.content)

    async def test_inconsistent_ranges_trigger_a_full_download(self):
        for invalid_header in (
            {"Content-Range": "bytes 4-7/11"},
            {"ETag": '"another-version"'},
        ):
            with self.subTest(invalid_header=invalid_header):
                self.requests.clear()
                self.output.unlink(missing_ok=True)

                def respond(request, invalid_header=invalid_header):
                    response = self.cdn_response(request)
                    if request.headers.get("Range") == "bytes=4-7":
                        response.headers.update(invalid_header)
                    return response

                await self.download(respond)
                self.assertIsNone(self.requests[-1])
                self.assertEqual(self.requests.count(None), 1)
                self.assertEqual(self.output.read_bytes(), self.content)

    async def test_same_size_corruption_on_disk_triggers_a_full_download(self):
        verify = self.plugin._verify_xhs_video_file

        async def change_first_file(path, size, digest):
            if None not in self.requests:
                path.write_bytes(b"x" + self.content[1:])
            await verify(path, size, digest)

        with patch.object(
            self.plugin, "_verify_xhs_video_file", side_effect=change_first_file
        ):
            await self.download(self.cdn_response)

        self.assertEqual(self.requests.count(None), 1)
        self.assertEqual(self.output.read_bytes(), self.content)
        self.assertFalse(self.temp_path.exists())

    async def test_partial_full_retry_is_rejected_and_cleaned_up(self):
        def respond(request):
            response = self.cdn_response(request)
            if request.headers.get("Range") == "bytes=0-3":
                del response.headers["Content-Range"]
            elif request.headers.get("Range") is None:
                return httpx.Response(206, content=self.content[:4])
            return response

        with self.assertRaisesRegex(handler._XhsVideoIntegrityError, "未返回完整文件"):
            await self.download(respond)

        self.assertEqual(self.requests, ["bytes=0-3", None])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.temp_path.exists())
