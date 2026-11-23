# region 导入
import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video

from ..common import (
    SizeLimitExceeded,
    get_xhs_card_path,
    get_xhs_comment_path,
    get_xhs_image_path,
    get_xhs_video_path,
)
from ..common.file_lifecycle import save_image_file
from ..common.image_compression import fit_image_file_limits
from ..common.qq_video_relay import (
    RelayVideoReferenceNode,
    send_components_to_private_relay,
)
from . import (
    XHS_HEADERS,
    XiaohongshuParseError,
    XiaohongshuResult,
    XiaohongshuRetryableError,
    extract_xhs_links,
)
from .extractor import _XHS_DOWNLOAD_UA, XiaohongshuExtractor

# endregion

# region 解析策略常量
XHS_PARSE_TIMEOUT_SEC = 30.0
XHS_PARSE_RETRY_BASE_DELAY_SEC = 1.0
XHS_PARSE_RETRY_MAX_DELAY_SEC = 8.0
XHS_ORIGINAL_IMAGE_MAX_BYTES = 10 * 1024 * 1024
XHS_VIDEO_DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
XHS_VIDEO_DOWNLOAD_WORKERS = 8
XHS_VIDEO_RANGE_RETRIES = 3
XHS_MERGE_FORWARD_MAX_NODES = 20
XHS_MERGE_FORWARD_CHUNK_DELAY_SEC = 1.0
# endregion


class _XhsVideoIntegrityError(RuntimeError):
    """下载响应或合并文件未通过完整性校验。"""


def _compress_xhs_original_if_needed(path: Path) -> Path:
    """Compress an oversized downloaded original image before sending it."""
    try:
        original_size = path.stat().st_size
    except OSError:
        return path

    if original_size <= XHS_ORIGINAL_IMAGE_MAX_BYTES:
        return path

    try:
        compressed_path = fit_image_file_limits(
            path,
            max_bytes=XHS_ORIGINAL_IMAGE_MAX_BYTES,
            max_height=0,
            force_jpeg=True,
        )
        compressed_size = compressed_path.stat().st_size
    except Exception as exc:
        logger.warning("⚠️ 小红书原图压缩失败，继续使用原文件: %s", str(exc))
        return path

    logger.info(
        "🍠 小红书原图超过 10MB，已压缩: %.1fMB -> %.1fMB, 文件=%s",
        original_size / 1024 / 1024,
        compressed_size / 1024 / 1024,
        compressed_path.name,
    )
    return compressed_path


# region 小红书混入
class XiaohongshuMixin:
    # region 路径与候选构建
    def _build_xhs_path(self, url: str, is_video: bool, request_id: str) -> Path:
        suffix = ".mp4" if is_video else self._guess_media_suffix(url, ".jpg")
        base_dir = get_xhs_video_path() if is_video else get_xhs_image_path()
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    @staticmethod
    def _build_xhs_live_gif_path(video_path: Path) -> Path:
        return video_path.with_suffix(".gif")

    def _build_xhs_card_path(self, source_url: str, request_id: str) -> Path:
        return (
            get_xhs_card_path() / f"{self._hash_url(source_url)}_{request_id}_card.png"
        )

    @staticmethod
    def _force_https(url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        if parsed.scheme in ("", "http"):
            return parsed._replace(scheme="https").geturl()
        return url

    # endregion

    # region 下载与渲染
    @staticmethod
    def _xhs_download_headers(referer: str | None) -> dict[str, str]:
        headers = dict(XHS_HEADERS)
        if referer:
            headers["Referer"] = referer
        headers["Origin"] = "https://www.xiaohongshu.com"
        headers["Accept"] = "*/*"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        return headers

    @staticmethod
    def _is_retryable_xhs_exception(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, XiaohongshuRetryableError)):
            return True
        text = str(exc).lower()
        retryable_patterns = (
            "timeout",
            "timed out",
            "connection",
            "reset",
            "refused",
            "temporary",
            "unavailable",
            "503",
            "502",
            "504",
            "429",
            "network",
        )
        return any(p in text for p in retryable_patterns)

    async def _download_xhs_video(
        self,
        urls: str | list[str],
        request_id: str,
        referer: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        candidates = [urls] if isinstance(urls, str) else urls
        candidates = list(dict.fromkeys(self._force_https(url) for url in candidates))
        if not candidates:
            raise RuntimeError("没有可用的小红书视频地址")
        cookies = XiaohongshuExtractor._parse_cookie_header(
            getattr(self, "xhs_cookies", "")
        )
        headers = self._xhs_download_headers(referer)
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        output_path = self._build_xhs_path(candidates[0], is_video=True, request_id=request_id)
        for index, url in enumerate(candidates):
            try:
                candidate_size = expected_size if index == 0 else None
                if candidate_size is None and max_bytes:
                    candidate_size = await self._probe_stream_size(
                        url, cookies=cookies, headers=headers
                    )
                if candidate_size is not None and max_bytes and candidate_size > max_bytes:
                    raise SizeLimitExceeded("超过大小限制")
                await self._download_xhs_video_parallel(
                    url, output_path, cookies=cookies, max_bytes=max_bytes, headers=headers
                )
                return output_path
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if index == len(candidates) - 1:
                    raise
                logger.warning(
                    "⚠️ 小红书视频候选下载失败，尝试下一地址 (%d/%d): %s",
                    index + 1,
                    len(candidates),
                    str(exc),
                )
        raise RuntimeError("小红书视频下载失败")

    async def _convert_xhs_live_photo_to_gif(self, video_path: Path) -> Path:
        """用 FFmpeg 将 Live Photo 短视频转换为循环 GIF。"""
        output_path = self._build_xhs_live_gif_path(video_path)
        temp_path = output_path.with_name(f"{output_path.stem}.part.gif")
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-an",
            "-filter_complex",
            "fps=12,scale=w='min(480,iw)':h=-2:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=128[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=3",
            "-loop",
            "0",
            str(temp_path),
        ]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode != 0:
                details = stderr.decode(errors="replace").strip()[-1000:]
                raise RuntimeError(details or "FFmpeg GIF 转换失败")
            if not temp_path.exists() or temp_path.stat().st_size == 0:
                raise RuntimeError("FFmpeg 未生成有效的 GIF 文件")
            await asyncio.to_thread(temp_path.replace, output_path)
            return output_path
        except BaseException:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            raise

    async def _download_xhs_video_parallel(
        self,
        url: str,
        output_path: Path,
        cookies: dict[str, str],
        max_bytes: int | None,
        headers: dict[str, str],
        workers: int = XHS_VIDEO_DOWNLOAD_WORKERS,
    ) -> int:
        """校验分段下载结果，有异常时重新请求完整文件。"""
        temp_path = output_path.with_suffix(output_path.suffix + ".part")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0),
                headers={**headers, "Accept-Encoding": "identity"},
                cookies=cookies or None,
            ) as client:
                try:
                    bytes_written = await self._download_xhs_video_ranges(
                        client, url, temp_path, max_bytes, workers
                    )
                except (_XhsVideoIntegrityError, httpx.RemoteProtocolError) as exc:
                    temp_path.unlink(missing_ok=True)
                    logger.warning(
                        "⚠️ 小红书视频完整性校验失败，改用单连接重新下载: %s",
                        str(exc),
                    )
                    async with client.stream(
                        "GET", url, follow_redirects=True
                    ) as response:
                        bytes_written = await self._download_xhs_video_full_response(
                            response, temp_path, max_bytes
                        )
                    logger.info(
                        "📥 小红书视频重新下载并通过校验: size=%.2fMB",
                        bytes_written / 1024 / 1024,
                    )
            temp_path.replace(output_path)
            return bytes_written
        finally:
            temp_path.unlink(missing_ok=True)

    async def _download_xhs_video_ranges(
        self,
        client: httpx.AsyncClient,
        url: str,
        temp_path: Path,
        max_bytes: int | None,
        workers: int,
    ) -> int:
        chunk_size = XHS_VIDEO_DOWNLOAD_CHUNK_BYTES
        async with client.stream(
            "GET",
            url,
            headers={"Range": f"bytes=0-{chunk_size - 1}"},
            follow_redirects=True,
        ) as response:
            if response.status_code == 200:
                bytes_written = await self._download_xhs_video_full_response(
                    response, temp_path, max_bytes
                )
                logger.debug(
                    "📥 XHS 视频服务器返回完整文件，已通过校验: %.2fMB",
                    bytes_written / 1024 / 1024,
                )
                return bytes_written

            if response.status_code == 416:
                raise _XhsVideoIntegrityError("CDN 已无法提供首段")
            response.raise_for_status()
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
            if response.status_code != 206 or not match:
                raise _XhsVideoIntegrityError(
                    f"首段响应缺少有效 Content-Range: {content_range!r}"
                )
            range_end, total_size = map(int, match.groups())
            if max_bytes and total_size > max_bytes:
                raise SizeLimitExceeded("超过大小限制")
            if total_size <= 0 or range_end != min(chunk_size, total_size) - 1:
                raise _XhsVideoIntegrityError(f"首段范围不一致: {content_range}")
            etag = response.headers.get("ETag", "")
            if total_size > chunk_size and not re.fullmatch(r'"[^"]*"', etag):
                raise _XhsVideoIntegrityError("缺少可用于比对分段版本的强 ETag")
            first_data = await response.aread()
            if len(first_data) != range_end + 1:
                raise _XhsVideoIntegrityError("首段内容长度与 Content-Range 不一致")

        ranges = [
            (start, min(start + chunk_size - 1, total_size - 1))
            for start in range(chunk_size, total_size, chunk_size)
        ]
        worker_count = max(1, min(workers, len(ranges) + 1))
        semaphore = asyncio.Semaphore(worker_count)

        async def fetch_range(start: int, end: int) -> bytes:
            expected_range = f"bytes {start}-{end}/{total_size}"
            async with semaphore:
                for attempt in range(XHS_VIDEO_RANGE_RETRIES):
                    try:
                        async with client.stream(
                            "GET",
                            url,
                            headers={"Range": f"bytes={start}-{end}"},
                            follow_redirects=True,
                        ) as response:
                            if response.status_code == 416:
                                raise _XhsVideoIntegrityError(
                                    f"CDN 已无法提供分段: bytes={start}-{end}"
                                )
                            response.raise_for_status()
                            content_range = response.headers.get("Content-Range")
                            if (
                                response.status_code != 206
                                or content_range != expected_range
                            ):
                                raise _XhsVideoIntegrityError(
                                    f"分段范围不一致: expected={expected_range}, "
                                    f"actual={content_range!r}"
                                )
                            if response.headers.get("ETag") != etag:
                                raise _XhsVideoIntegrityError(
                                    f"分段 ETag 与首段不一致: bytes={start}-{end}"
                                )
                            data = await response.aread()
                            if len(data) != end - start + 1:
                                raise _XhsVideoIntegrityError(
                                    f"分段内容长度与 Content-Range 不一致: bytes={start}-{end}"
                                )
                            return data
                    except (httpx.RequestError, httpx.HTTPStatusError):
                        if attempt == XHS_VIDEO_RANGE_RETRIES - 1:
                            raise
                        await asyncio.sleep(2**attempt)
            raise RuntimeError(f"视频分段下载失败: bytes={start}-{end}")

        tasks = [asyncio.create_task(fetch_range(start, end)) for start, end in ranges]
        try:
            parts = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        digest = hashlib.sha256()
        with temp_path.open("wb") as file:
            for part in (first_data, *parts):
                digest.update(part)
                await asyncio.to_thread(file.write, part)
        await self._verify_xhs_video_file(temp_path, total_size, digest.digest())
        logger.info(
            "📥 XHS 视频分段并发下载完成: size=%.2fMB, segments=%d, workers=%d, cookie=%s, 已校验 SHA-256",
            total_size / 1024 / 1024,
            len(ranges) + 1,
            worker_count,
            "on" if client.cookies else "off",
        )
        return total_size

    async def _download_xhs_video_full_response(
        self,
        response: httpx.Response,
        temp_path: Path,
        max_bytes: int | None,
    ) -> int:
        response.raise_for_status()
        if response.status_code != 200 or "Content-Range" in response.headers:
            raise _XhsVideoIntegrityError(
                f"重新下载未返回完整文件: HTTP {response.status_code}"
            )
        length = response.headers.get("Content-Length")
        total_size = int(length) if length is not None else None
        if max_bytes and total_size is not None and total_size > max_bytes:
            raise SizeLimitExceeded("超过大小限制")
        bytes_written = 0
        digest = hashlib.sha256()
        with temp_path.open("wb") as file:
            async for chunk in response.aiter_bytes(1024 * 1024):
                bytes_written += len(chunk)
                if max_bytes and bytes_written > max_bytes:
                    raise SizeLimitExceeded("超过大小限制")
                digest.update(chunk)
                await asyncio.to_thread(file.write, chunk)
        await self._verify_xhs_video_file(
            temp_path,
            total_size if total_size is not None else bytes_written,
            digest.digest(),
        )
        return bytes_written

    @staticmethod
    async def _verify_xhs_video_file(
        path: Path, expected_size: int, expected_digest: bytes
    ) -> None:
        actual_size = path.stat().st_size
        if expected_size <= 0 or actual_size != expected_size:
            raise _XhsVideoIntegrityError(
                f"文件大小不一致: expected={expected_size}, actual={actual_size}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := await asyncio.to_thread(
                file.read, XHS_VIDEO_DOWNLOAD_CHUNK_BYTES
            ):
                digest.update(chunk)
        if digest.digest() != expected_digest:
            raise _XhsVideoIntegrityError("写入文件与收到的数据 SHA-256 不一致")

    async def _download_xhs_image(
        self,
        urls: str | list[str],
        request_id: str,
        file_id: str | None = None,
        referer: str | None = None,
    ) -> Path:
        """下载图片 - 两级回退策略

        1. 如果开启原图下载：按配置顺序尝试 CDN 原图 / CI-PNG 转码
           （xhs_prefer_ci_png=True 时 CI-PNG 优先，否则 CDN 优先）
        2. 如果都失败：回退到多 CDN 兜底策略
        """
        start_time = time.perf_counter()
        source_candidates = (
            [urls] if isinstance(urls, str) else list(dict.fromkeys(urls))
        )
        if not source_candidates:
            raise RuntimeError("没有可用的小红书图片地址")
        url = source_candidates[0]

        output_path = self._build_xhs_path(url, is_video=False, request_id=request_id)

        # 提取 image token (参考 XHS-Downloader)
        token = self._extract_image_token(url)
        logger.debug(
            "XHS 图片下载开始: url=%s, file_id=%s, token=%s", url[:80], file_id, token
        )

        # region 原图下载尝试
        if getattr(self, "xhs_download_original", True) and token:
            original_start = time.perf_counter()

            # 构建原图 URL 候选列表
            original_candidates = []

            # CDN 直接链接 - 返回小红书存储的原始文件（通常为 JPEG）
            cdn_domains = [
                "sns-img-bd.xhscdn.com",
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
            ]
            cdn_candidates = [
                {
                    "url": f"https://{domain}/{token}",
                    "desc": f"CDN-{domain.split('-')[2].split('.')[0]}-auto",
                    "format": None,
                }
                for domain in cdn_domains
            ]

            # CI 转码链接 - 通过腾讯云 CI 将图片转为 PNG（体积约为原图 2 倍，画质一致）
            ci_candidates = [
                {
                    "url": f"https://ci.xiaohongshu.com/{token}?imageView2/format/png",
                    "desc": "CI-PNG-原图",
                    "format": "png",
                },
            ]

            if getattr(self, "xhs_prefer_ci_png", False):
                original_candidates = ci_candidates + cdn_candidates
            else:
                original_candidates = cdn_candidates + ci_candidates

            retry_count = max(0, int(getattr(self, "retry_count", 3)))
            for cand in original_candidates:
                cand_url = cand["url"]
                desc = cand["desc"]
                format_name = cand["format"]

                for attempt in range(retry_count + 1):
                    attempt_start = time.perf_counter()
                    try:
                        timeout = aiohttp.ClientTimeout(total=600, connect=60)
                        headers = {
                            "User-Agent": _XHS_DOWNLOAD_UA,
                            "Referer": "https://www.xiaohongshu.com/",
                        }

                        async with aiohttp.ClientSession(
                            headers=headers, timeout=timeout
                        ) as session:
                            async with session.get(cand_url) as resp:
                                attempt_elapsed = time.perf_counter() - attempt_start

                                if resp.status == 200:
                                    # 先写入临时文件，避免一次性读取导致 payload 不完整
                                    temp_output = output_path.with_suffix(".tmp")
                                    temp_path = temp_output.with_suffix(
                                        temp_output.suffix + ".part"
                                    )
                                    content_len = 0
                                    f = None
                                    try:

                                        def _open_temp():
                                            temp_path.parent.mkdir(
                                                parents=True, exist_ok=True
                                            )
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_temp)
                                        try:
                                            async for (
                                                chunk
                                            ) in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        # 验证文件大小（至少 10KB 才认为是有效图片）
                                        if (
                                            content_len >= 10 * 1024
                                            and temp_path.exists()
                                        ):
                                            # 确定输出文件后缀
                                            if format_name:
                                                actual_suffix = f".{format_name}"
                                            else:

                                                def _read_head():
                                                    with open(temp_path, "rb") as rf:
                                                        return rf.read(32)

                                                head = await asyncio.to_thread(
                                                    _read_head
                                                )
                                                actual_suffix = (
                                                    self._detect_image_suffix(
                                                        head,
                                                        resp.headers.get(
                                                            "Content-Type"
                                                        ),
                                                    )
                                                )

                                            final_output = output_path.with_suffix(
                                                actual_suffix
                                            )
                                            final_part = final_output.with_suffix(
                                                final_output.suffix + ".part"
                                            )

                                            def _move():
                                                if final_part.exists():
                                                    final_part.unlink()
                                                temp_path.replace(final_part)
                                                final_part.replace(final_output)

                                            await asyncio.to_thread(_move)
                                            final_output = await asyncio.to_thread(
                                                _compress_xhs_original_if_needed,
                                                final_output,
                                            )

                                            total_elapsed = (
                                                time.perf_counter() - start_time
                                            )
                                            logger.debug(
                                                "XHS 原图下载成功 (%s): size=%.1fMB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                                desc,
                                                content_len / 1024 / 1024,
                                                attempt_elapsed,
                                                total_elapsed,
                                            )
                                            return final_output
                                        else:
                                            logger.debug(
                                                "XHS 原图响应过小 (%s): size=%d bytes, 耗时=%.2fs",
                                                desc,
                                                content_len,
                                                attempt_elapsed,
                                            )
                                    finally:
                                        if (
                                            temp_path.exists()
                                            and content_len < 10 * 1024
                                        ):
                                            try:
                                                temp_path.unlink()
                                            except Exception:
                                                pass
                                else:
                                    logger.debug(
                                        "XHS 原图请求失败 (%s): HTTP %d, 耗时=%.2fs",
                                        desc,
                                        resp.status,
                                        attempt_elapsed,
                                    )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        attempt_elapsed = time.perf_counter() - attempt_start
                        logger.debug(
                            "XHS 原图下载异常 (%s): %s, 耗时=%.2fs",
                            desc,
                            str(e)[:50],
                            attempt_elapsed,
                        )

                    if attempt < retry_count:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)

            original_elapsed = time.perf_counter() - original_start
            logger.debug(
                "XHS 原图下载全部失败，回退到普通策略，原图尝试耗时=%.2fs",
                original_elapsed,
            )
        # endregion

        # region CDN 兜底策略

        # 基础 Headers
        base_headers = {
            "User-Agent": _XHS_DOWNLOAD_UA,
        }

        # 构建阶梯候选列表
        candidates = []

        # 1. 原始 URL (带签名)
        for source_index, source_url in enumerate(source_candidates, start=1):
            raw_url = (
                source_url.replace("http://", "https://", 1)
                if source_url.startswith("http://")
                else source_url
            )
            candidates.append({"url": raw_url, "desc": f"Raw-{source_index}"})

        effective_id = file_id or token
        if effective_id:
            # 2. 无签名通用 CDN (兜底方案)
            domains = [
                "sns-img-bd.xhscdn.com",
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
                "sns-webpic-qc.xhscdn.com",
            ]
            for domain in domains:
                for path_prefix in ["", "spectrum/"]:
                    path = f"{path_prefix}{effective_id}"
                    candidates.append(
                        {
                            "url": f"https://{domain}/{path}",
                            "desc": f"CDN-{domain.split('.')[0]}",
                        }
                    )

        errors = []
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        for cand in candidates:
            cand_url = cand["url"]
            desc = cand["desc"]

            # 两种 header 变体
            header_variants = [
                {**base_headers, "Referer": "https://www.xiaohongshu.com/"},
                base_headers.copy(),
            ]

            for hv in header_variants:
                for attempt in range(retry_count + 1):
                    attempt_start = time.perf_counter()
                    try:
                        # 超长超时
                        timeout = aiohttp.ClientTimeout(total=300, connect=30)
                        async with aiohttp.ClientSession(
                            headers=hv, timeout=timeout
                        ) as session:
                            async with session.get(cand_url) as resp:
                                if resp.status == 200:
                                    temp_path = output_path.with_suffix(
                                        output_path.suffix + ".part"
                                    )
                                    content_len = 0
                                    f = None
                                    try:

                                        def _open_part():
                                            temp_path.parent.mkdir(
                                                parents=True, exist_ok=True
                                            )
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_part)
                                        try:
                                            async for (
                                                chunk
                                            ) in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        if content_len >= 1024 and temp_path.exists():

                                            def _move():
                                                temp_path.replace(output_path)

                                            await asyncio.to_thread(_move)

                                            attempt_elapsed = (
                                                time.perf_counter() - attempt_start
                                            )
                                            total_elapsed = (
                                                time.perf_counter() - start_time
                                            )
                                            logger.debug(
                                                "📥 XHS CDN 图片下载成功 (%s): size=%.1fKB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                                desc,
                                                content_len / 1024,
                                                attempt_elapsed,
                                                total_elapsed,
                                            )
                                            return output_path
                                        else:
                                            logger.debug(
                                                "XHS CDN 图片过小，忽略 (%s): size=%d bytes",
                                                desc,
                                                content_len,
                                            )
                                            errors.append(
                                                f"{desc}: 文件过小 ({content_len} bytes)"
                                            )
                                            if temp_path.exists():
                                                await asyncio.to_thread(
                                                    temp_path.unlink
                                                )
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception:
                                        if temp_path.exists():
                                            try:
                                                await asyncio.to_thread(
                                                    temp_path.unlink
                                                )
                                            except Exception:
                                                pass
                                        raise

                                errors.append(f"{desc}: HTTP {resp.status}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        errors.append(f"{desc}: {str(e)[:20]}")

                    if attempt < retry_count:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)
        # endregion

        # 全部失败
        total_elapsed = time.perf_counter() - start_time
        error_summary = " | ".join(errors[:5])  # 只取前5个错误
        logger.error(
            "❌ XHS 图片下载全线失败: 总耗时=%.2fs, 错误=%s",
            total_elapsed,
            error_summary,
        )
        raise RuntimeError(f"图片下载失败: {error_summary}")

    @staticmethod
    def _extract_image_token(url: str) -> str | None:
        """从 URL 中提取 image token（参考 XHS-Downloader）"""
        if not url:
            return None
        try:
            # 格式: https://xxx.xhscdn.com/spectrum/1040g0k... 或类似
            # 提取路径第5个/之后的部分，去掉!后缀
            parts = url.split("/")
            if len(parts) >= 6:
                token = "/".join(parts[5:]).split("!")[0].split("?")[0]
                if token and len(token) > 10:
                    return token
            # 备用方案：直接取最后一段
            last_part = url.split("/")[-1].split("!")[0].split("?")[0]
            if last_part and len(last_part) > 10:
                return last_part
        except Exception:
            pass
        return None

    @staticmethod
    def _detect_image_suffix(content: bytes, content_type: str | None) -> str:
        """从文件签名或 Content-Type 检测图片格式"""
        # 文件签名检测（魔数）
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if content[:3] == b"\xff\xd8\xff":
            return ".jpeg"
        if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp"
        if content[:4] == b"GIF8":
            return ".gif"
        if content[4:12] in (b"ftypavif", b"ftypavis"):
            return ".avif"
        if content[4:12] in (b"ftypheic", b"ftypmif1"):
            return ".heic"

        # Content-Type 检测
        if content_type:
            ct = content_type.lower()
            if "png" in ct:
                return ".png"
            if "jpeg" in ct or "jpg" in ct:
                return ".jpeg"
            if "webp" in ct:
                return ".webp"
            if "gif" in ct:
                return ".gif"

        # 默认 jpeg
        return ".jpeg"

    @staticmethod
    def _clean_xhs_summary_text(text: str) -> str:
        # 小红书原始 desc / shareDesc 的话题标签通常形如 #xxx[话题]#。
        # 文字摘要直接展示原文时会把这个内部标记带出来，因此仅在摘要里清洗成 #xxx#。
        return re.sub(r"#([^#\n]+?)\[话题\]#", r"#\1#", text)

    @staticmethod
    def _clean_xhs_display_link(link: str) -> str:
        # 仅清洗摘要展示用链接，不影响真实解析请求。
        # JSON 卡片里的 jumpUrl 常带 app_platform/xsec_token/share_channel 等参数，展示时去掉后缀更简洁。
        try:
            parsed = urlparse(link)
        except Exception:
            return link
        host = parsed.netloc.lower()
        if "xiaohongshu.com" not in host:
            return link
        return parsed._replace(query="", fragment="").geturl()

    def _build_xhs_summary(
        self,
        result: XiaohongshuResult,
        *,
        image_count: int,
        is_video: bool,
        display_link: str | None = None,
    ) -> str:
        author = result.author or "未知作者"
        title = result.title or "未知标题"
        lines = ["🍠 小红书", f"作者：{author}", f"标题：{title}"]
        if result.text:
            lines.append(
                f"正文：{XiaohongshuMixin._clean_xhs_summary_text(result.text)}"
            )
        lines.append(f"媒体：{'视频' if is_video else f'图片 {image_count} 张'}")
        summary_link = display_link or result.source_url
        if summary_link:
            lines.append(
                f"链接：{XiaohongshuMixin._clean_xhs_display_link(summary_link)}"
            )
        return "\n".join(lines)

    async def _render_xhs_card(
        self,
        result: XiaohongshuResult,
        image_paths: list[Path],
        cover_path: Path | None,
        is_video: bool,
        request_id: str,
    ) -> Path | None:
        try:
            card_path = self._build_xhs_card_path(result.source_url, request_id)
            image = await asyncio.to_thread(
                self.xhs_renderer.render,
                title=result.title,
                author=result.author,
                text=result.text,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video,
            )
            await save_image_file(image, card_path, format="PNG")
            return card_path
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 小红书卡片渲染失败: %s", str(exc))
            return None

    async def _capture_xhs_comment_screenshots(
        self,
        result: XiaohongshuResult,
        target_link: str,
        request_id: str,
    ) -> list[Path]:
        if not getattr(self, "xhs_enable_comment_screenshot", False):
            return []
        screenshotter = getattr(self, "xhs_comment_screenshotter", None)
        if screenshotter is None:
            return []
        try:
            paths = await asyncio.wait_for(
                screenshotter.capture(
                    source_url=target_link or result.source_url,
                    note_id=result.note_id,
                    output_dir=get_xhs_comment_path(),
                    request_id=request_id,
                    max_comments=getattr(self, "xhs_comment_screenshot_max", 20),
                    mode=getattr(self, "xhs_comment_screenshot_mode", "网页截图"),
                    cookies_text=getattr(self, "xhs_cookies", ""),
                ),
                timeout=90.0,
            )
            if paths:
                logger.info("🍠 小红书评论截图完成: %d 张", len(paths))
            return paths
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 小红书评论截图失败，已跳过: %s", str(exc))
            return []

    async def _relay_xhs_components(
        self,
        event: AstrMessageEvent,
        components: list[object],
        source_tag: str = "",
    ) -> list[RelayVideoReferenceNode] | None:
        relay_fn = getattr(self, "_relay_component_reference_nodes", None)
        if callable(relay_fn):
            return await relay_fn(event, components, source_tag)

        relay_umo = str(getattr(self, "video_relay_umo", "") or "").strip()
        if not relay_umo or not components:
            return None

        try:
            references = await send_components_to_private_relay(
                event, relay_umo, components
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "⚠️ 小红书内容中转失败%s，回退为逐条发送: %s",
                source_tag,
                str(exc),
            )
            return None

        logger.debug(
            "📨 小红书内容已上传到 QQ 私聊中转%s: 数量=%d, UMO=%s",
            source_tag,
            len(references),
            relay_umo,
        )
        return references

    # endregion

    # region 小红书处理
    async def _process_xhs(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ):
        media_paths: list[Path] = []
        try:
            async for result in XiaohongshuMixin._process_xhs_inner(
                self, event, target_link, is_from_card, media_paths
            ):
                yield result
        finally:
            if media_paths:
                await self.cleanup_files(media_paths, [])

    async def _process_xhs_inner(
        self,
        event: AstrMessageEvent,
        target_link: str,
        is_from_card: bool,
        media_paths: list[Path],
    ):
        process_start = time.perf_counter()
        timing = {}  # 记录各步骤耗时

        self._refresh_config()
        if not self.xhs_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]

        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()

        if not target_link:
            logger.warning("⚠️ 小红书链接为空%s", source_tag)
            return
        logger.info("🍠 小红书解析%s: %s", source_tag, target_link)

        # region 解析阶段
        parse_start = time.perf_counter()
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        result: XiaohongshuResult | None = None
        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.xhs_extractor.parse(target_link),
                    timeout=XHS_PARSE_TIMEOUT_SEC,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ 小红书解析任务已中断%s", source_tag)
                return
            except XiaohongshuParseError as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning(
                        "⚠️ 小红书解析失败%s: %s，%.1fs后重试 (%d/%d)",
                        source_tag,
                        str(exc),
                        wait_time,
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析失败%s: %s", source_tag, str(exc))
                return
            except Exception as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning(
                        "⚠️ 小红书解析异常%s: %s，%.1fs后重试 (%d/%d)",
                        source_tag,
                        str(exc),
                        wait_time,
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析异常%s: %s", source_tag, str(exc))
                return

        if result is None:
            logger.error(
                "❌ 小红书解析最终失败%s: %s, link=%s, timeout=%.0fs, retries=%d",
                source_tag,
                str(last_error) if last_error else "unknown",
                target_link,
                XHS_PARSE_TIMEOUT_SEC,
                retry_count,
            )
            return

        timing["parse"] = time.perf_counter() - parse_start
        # endregion

        logger.debug(
            "🍠 小红书解析完成%s: 视频=%s, 图片=%s, 动图=%s, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            len(result.live_photo_urls),
            timing["parse"],
        )

        title = result.title or "未知标题"

        if (
            not result.video_url
            and not result.image_urls
            and not result.live_photo_urls
        ):
            logger.warning("⚠️ 小红书未找到可下载的媒体%s: %s", source_tag, target_link)
            return

        media_components: list[object] = []
        intermediate_paths: set[Path] = set()
        image_paths: list[Path] = []
        image_paths_by_index: dict[int, Path] = {}
        cover_path: Path | None = None
        failed_images = 0
        failed_live_photos = 0

        # region 下载阶段
        download_start = time.perf_counter()

        # 视频笔记：优先下载视频
        if result.video_url:
            try:
                selected_stream = next(
                    (
                        stream
                        for stream in getattr(result, "video_streams", [])
                        if stream.url == result.video_url
                    ),
                    None,
                )
                video_path = await self._download_xhs_video(
                    result.video_urls or result.video_url,
                    request_id,
                    referer=result.source_url,
                    expected_size=getattr(selected_stream, "size", None),
                )
                media_paths.append(video_path)
                media_components.append(Video.fromFileSystem(str(video_path.resolve())))
                # 下载封面图
                cover_url = result.cover_url or (
                    result.image_urls[0] if result.image_urls else None
                )
                need_cover = bool(
                    cover_url
                    and getattr(self, "xhs_render_card", False)
                    and (
                        getattr(self, "xhs_merge_send", False)
                        or getattr(self, "xhs_enable_comment_screenshot", False)
                    )
                )
                if need_cover:
                    try:
                        cover_candidates = (
                            result.image_url_candidates[0]
                            if result.image_url_candidates
                            and result.image_urls
                            and cover_url == result.image_urls[0]
                            else [cover_url]
                        )
                        cover_path = await self._download_xhs_image(
                            cover_candidates,
                            request_id,
                            referer=result.source_url,
                        )
                        media_paths.append(cover_path)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "⚠️ 小红书封面下载失败%s: %s", source_tag, str(exc)
                        )
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ 小红书视频大小超过限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                return
            except Exception as exc:
                logger.error("❌ 小红书视频下载失败%s: %s", source_tag, str(exc))
                return
        # 图片笔记：下载图片
        elif result.image_urls:
            image_urls = result.image_urls[: self.xhs_max_media]
            file_ids = result.file_ids[: self.xhs_max_media] if result.file_ids else []
            if getattr(self, "xhs_concurrent_download", False):
                # 并发下载
                async def _download_one(
                    i: int, url: str
                ) -> tuple[int, Path | None, Exception | None]:
                    file_id = file_ids[i] if i < len(file_ids) else None
                    candidates = (
                        result.image_url_candidates[i]
                        if i < len(result.image_url_candidates)
                        else [url]
                    )
                    try:
                        path = await self._download_xhs_image(
                            candidates,
                            request_id,
                            file_id=file_id,
                            referer=result.source_url,
                        )
                        media_paths.append(path)
                        return (i, path, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        return (i, None, exc)

                dl_results = await asyncio.gather(
                    *[_download_one(i, url) for i, url in enumerate(image_urls)]
                )
                # 按原始顺序组装结果
                for i, path, exc in sorted(dl_results, key=lambda item: item[0]):
                    if path is not None:
                        image_paths.append(path)
                        image_paths_by_index[i] = path
                        media_components.append(
                            Image.fromFileSystem(str(path.resolve()))
                        )
                    else:
                        failed_images += 1
                        logger.warning(
                            "⚠️ 小红书图片下载失败%s [%d/%d]: %s",
                            source_tag,
                            i + 1,
                            len(image_urls),
                            str(exc),
                        )

            else:
                # 串行下载
                for i, url in enumerate(image_urls):
                    try:
                        file_id = file_ids[i] if i < len(file_ids) else None
                        candidates = (
                            result.image_url_candidates[i]
                            if i < len(result.image_url_candidates)
                            else [url]
                        )
                        image_path = await self._download_xhs_image(
                            candidates,
                            request_id,
                            file_id=file_id,
                            referer=result.source_url,
                        )
                        image_paths.append(image_path)
                        image_paths_by_index[i] = image_path
                        media_paths.append(image_path)
                        media_components.append(
                            Image.fromFileSystem(str(image_path.resolve()))
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed_images += 1
                        logger.warning(
                            "⚠️ 小红书图片下载失败%s [%d/%d]: %s",
                            source_tag,
                            i + 1,
                            len(image_urls),
                            str(exc),
                        )

        # Live 图始终先发送上面下载的静态图片，再附加配置选择的动态格式。
        if getattr(self, "xhs_enable_live_photo", False) and not result.video_url:
            live_photo_video_urls = getattr(result, "live_photo_video_urls", [])
            if not isinstance(live_photo_video_urls, list):
                live_photo_video_urls = []
            live_format = getattr(self, "xhs_live_photo_format", "视频")
            live_video_candidates = [
                (image_index, live_photo_video_urls[image_index])
                for image_index in sorted(image_paths_by_index)
                if image_index < len(live_photo_video_urls)
                and isinstance(live_photo_video_urls[image_index], str)
                and live_photo_video_urls[image_index]
            ]
            remaining = max(self.xhs_max_media - len(result.image_urls), 0)
            unpaired_live_urls = [
                url for url in result.live_photo_urls if url not in live_photo_video_urls
            ][:remaining]
            live_video_candidates.extend(
                enumerate(unpaired_live_urls, start=len(result.image_urls))
            )
            candidates_by_url = dict(
                zip(result.live_photo_urls, result.live_photo_url_candidates)
            )
            live_video_limit = max(
                0, int(getattr(self, "xhs_live_photo_video_limit", 1))
            )
            if live_video_limit > 0 and len(live_video_candidates) > live_video_limit:
                skipped_count = len(live_video_candidates) - live_video_limit
                logger.info(
                    "🍠 小红书 Live 图动态超过每篇上限: 候选=%d, 上限=%d, 跳过=%d",
                    len(live_video_candidates),
                    live_video_limit,
                    skipped_count,
                )
                live_video_candidates = live_video_candidates[:live_video_limit]

            for image_index, live_video_url in live_video_candidates:
                try:
                    live_request_id = f"{request_id}_live_{image_index}"
                    live_video_path = await self._download_xhs_video(
                        candidates_by_url.get(live_video_url) or live_video_url,
                        live_request_id,
                        referer=result.source_url,
                    )
                    media_paths.append(live_video_path)
                    if live_format == "GIF":
                        intermediate_paths.add(live_video_path)
                        live_media_path = await self._convert_xhs_live_photo_to_gif(
                            live_video_path
                        )
                        media_paths.append(live_media_path)
                        media_components.append(
                            Image.fromFileSystem(str(live_media_path.resolve()))
                        )
                    else:
                        media_components.append(
                            Video.fromFileSystem(str(live_video_path.resolve()))
                        )
                    logger.debug(
                        "🍠 小红书 Live 图动态媒体已准备: image=%d, format=%s",
                        image_index + 1,
                        live_format,
                    )
                except asyncio.CancelledError:
                    raise
                except SizeLimitExceeded:
                    failed_live_photos += 1
                    logger.warning(
                        "⚠️ 小红书 Live 图短视频超过大小限制%s (序号 %d)",
                        source_tag,
                        image_index + 1,
                    )
                except Exception as exc:
                    failed_live_photos += 1
                    logger.warning(
                        "⚠️ 小红书 Live 图动态转换/下载失败%s (序号 %d): %s",
                        source_tag,
                        image_index + 1,
                        str(exc),
                    )

        timing["download"] = time.perf_counter() - download_start
        # endregion

        if not media_components:
            logger.debug(
                "XHS 无媒体下载成功%s: url=%s, 失败图片=%d, 下载耗时=%.2fs",
                source_tag,
                result.source_url,
                failed_images,
                timing["download"],
            )
            return

        comment_screenshot_enabled = bool(
            getattr(self, "xhs_enable_comment_screenshot", False)
        )
        summary_enabled = (
            bool(image_paths)
            or bool(
                result.video_url
                and (self.xhs_merge_send or bool(getattr(self, "video_relay_umo", "")))
            )
            or comment_screenshot_enabled
        )
        render_card = getattr(self, "xhs_render_card", False)
        is_video_post = bool(result.video_url and not image_paths)
        summary_text = None
        if summary_enabled and not render_card:
            summary_text = self._build_xhs_summary(
                result,
                image_count=len(image_paths),
                is_video=is_video_post,
                display_link=target_link,
            )

        # region 渲染阶段
        render_start = time.perf_counter()
        card_path = None
        if summary_enabled and render_card:
            card_path = await self._render_xhs_card(
                result,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video_post,
                request_id=request_id,
            )
        if card_path:
            media_paths.append(card_path)
            media_components.insert(0, Image.fromFileSystem(str(card_path.resolve())))

        try:
            comment_paths = await self._capture_xhs_comment_screenshots(
                result, target_link, request_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 小红书评论截图失败，已跳过: %s", str(exc))
            comment_paths = []
        if comment_paths:
            media_paths.extend(comment_paths)
            comment_components = [
                Image.fromFileSystem(str(path.resolve())) for path in comment_paths
            ]
            insert_index = 1 if card_path else 0
            media_components[insert_index:insert_index] = comment_components
        timing["render"] = time.perf_counter() - render_start
        # endregion

        # region 发送阶段
        send_start = time.perf_counter()

        # 转换超过QQ限制的图片为文件组件
        def _convert_to_file_if_needed(component):
            """如果图片超过QQ限制，转为 File 组件上传"""
            if (
                isinstance(component, Image)
                and hasattr(component, "path")
                and component.path
            ):
                try:
                    qq_image_size_limit_mb = getattr(
                        self, "xhs_qq_image_size_limit_mb", 30
                    )
                    if qq_image_size_limit_mb <= 0:
                        return component
                    file_size = Path(component.path).stat().st_size
                    if file_size > qq_image_size_limit_mb * 1024 * 1024:
                        file_name = Path(component.path).name
                        logger.info(
                            "XHS 图片 %.1fMB 超过 %dMB QQ限制，转为文件上传: %s",
                            file_size / 1024 / 1024,
                            qq_image_size_limit_mb,
                            file_name,
                        )
                        return File(file=component.path, name=file_name)
                except Exception:
                    pass
            return component

        media_components = [_convert_to_file_if_needed(c) for c in media_components]
        standalone_video_components = [
            component for component in media_components if isinstance(component, Video)
        ]
        chat_record_components = [
            component
            for component in media_components
            if not isinstance(component, Video)
        ]

        # 计算总大小
        total_size_bytes = await asyncio.to_thread(
            lambda: sum(
                p.stat().st_size
                for p in media_paths
                if p not in intermediate_paths and p.exists()
            )
        )
        total_size_mb = total_size_bytes / (1024 * 1024)

        # 判断是否触发解合阈值
        threshold = getattr(self, "xhs_auto_unmerge_threshold_mb", 20)
        exceed_threshold = threshold > 0 and total_size_mb > threshold
        force_unmerge = False
        relay_merge_nodes = None

        # 判断是否为图文笔记（有图片路径）
        is_image_post = bool(image_paths)

        if exceed_threshold:
            if getattr(self, "video_relay_umo", "") and (
                is_image_post
                or self.xhs_merge_send
                or bool(standalone_video_components)
            ):
                relay_components: list[object] = []
                if summary_text:
                    relay_components.append(Plain(summary_text))
                relay_components.extend(chat_record_components)
                relay_components.extend(standalone_video_components)
                logger.info(
                    "🍠 XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB)，发送至中转账号后合并转发: 条目=%d",
                    total_size_mb,
                    threshold,
                    len(relay_components),
                )
                relay_merge_nodes = await XiaohongshuMixin._relay_xhs_components(
                    self, event, relay_components, source_tag
                )
            if relay_merge_nodes is None:
                logger.info(
                    "XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB)，强制逐条发送",
                    total_size_mb,
                    threshold,
                )
                force_unmerge = True

        # 图文笔记始终合并转发；视频笔记根据配置决定
        # force_unmerge 仅在未配置中转或中转失败时对图文笔记生效（逐条发送图片）
        if is_image_post:
            # 图文笔记：始终合并转发（除非触发解合阈值且无法中转合并）
            should_merge = not force_unmerge
        else:
            # 视频笔记：根据配置决定
            should_merge = self.xhs_merge_send

        relay_video_nodes = None
        if (
            relay_merge_nodes is None
            and standalone_video_components
            and not (force_unmerge and is_image_post)
            and getattr(self, "video_relay_umo", "")
        ):
            relay_video_nodes = await self._relay_video_reference_nodes(
                event, standalone_video_components, source_tag
            )
        relay_video_ready = (
            relay_merge_nodes is not None or relay_video_nodes is not None
        )
        should_merge = should_merge or relay_video_ready

        if should_merge:
            if relay_merge_nodes is not None:
                merge_nodes = list(relay_merge_nodes)
            else:
                merge_nodes = []
                sender_uin = self._get_merge_sender_uin(event)
                if summary_text:
                    merge_nodes.append(
                        Node(uin=sender_uin, content=[Plain(summary_text)])
                    )
                for component in chat_record_components:
                    merge_component = await self._prepare_component_for_merge_send(
                        component
                    )
                    merge_nodes.append(Node(uin=sender_uin, content=[merge_component]))
                if relay_video_nodes:
                    merge_nodes.extend(relay_video_nodes)
            if len(merge_nodes) > XHS_MERGE_FORWARD_MAX_NODES:
                logger.info(
                    "🍠 小红书合并转发共 %d 个节点，将拆分为每条最多 %d 个节点发送",
                    len(merge_nodes),
                    XHS_MERGE_FORWARD_MAX_NODES,
                )
            for start in range(0, len(merge_nodes), XHS_MERGE_FORWARD_MAX_NODES):
                nodes = Nodes(
                    merge_nodes[start : start + XHS_MERGE_FORWARD_MAX_NODES]
                )
                yield event.chain_result([nodes])
                if start + XHS_MERGE_FORWARD_MAX_NODES < len(merge_nodes):
                    await asyncio.sleep(XHS_MERGE_FORWARD_CHUNK_DELAY_SEC)
            if standalone_video_components and not relay_video_ready:
                if merge_nodes:
                    await asyncio.sleep(2.0)
                for i, component in enumerate(standalone_video_components):
                    yield event.chain_result([component])
                    if i < len(standalone_video_components) - 1:
                        await asyncio.sleep(2.0)
        else:
            if is_image_post:
                # 图文笔记逐条发送（触发解合阈值时）
                if summary_text:
                    yield event.chain_result([Plain(summary_text)])
                for i, component in enumerate(media_components):
                    yield event.chain_result([component])
                    if i < len(media_components) - 1:
                        await asyncio.sleep(2.0)
            else:
                if summary_text and (
                    is_image_post
                    or self.xhs_merge_send
                    or comment_screenshot_enabled
                ):
                    yield event.chain_result([Plain(summary_text)])
                prefix_components = [
                    component
                    for component in media_components
                    if not isinstance(component, Video)
                ]
                if (
                    card_path
                    and not self.xhs_merge_send
                    and not comment_screenshot_enabled
                ):
                    card_path_value = str(card_path.resolve())
                    prefix_components = [
                        component
                        for component in prefix_components
                        if getattr(component, "path", "") != card_path_value
                    ]
                for component in prefix_components:
                    yield event.chain_result([component])
                    await asyncio.sleep(2.0)
                for component in media_components:
                    if isinstance(component, Video):
                        yield event.chain_result([component])
                        break

        timing["send"] = time.perf_counter() - send_start
        # endregion

        # 输出完整耗时日志
        total_elapsed = time.perf_counter() - process_start
        logger.info(
            "🍠 XHS 处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 渲染=%.2fs, 发送=%.2fs, 总计=%.2fs",
            source_tag,
            title[:20],
            len(media_components),
            failed_images + failed_live_photos,
            timing.get("parse", 0),
            timing.get("download", 0),
            timing.get("render", 0),
            timing.get("send", 0),
            total_elapsed,
        )

    # endregion

    # region 事件处理器
    # 事件过滤器由 main.py 注册，确保绑定插件实例。
    async def handle_xhs(self, event: AstrMessageEvent):
        if not self.xhs_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_xhs_links(event.message_str)
        logger.info("🍠 小红书匹配链接: %s", links)
        if not links:
            return
        try:
            async for result in self._process_xhs(event, links[0], is_from_card=False):
                yield result
        except asyncio.CancelledError:
            logger.info("♻️ 小红书解析任务已中断")
            return

    # endregion


# endregion
