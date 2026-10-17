"""微博消息处理器。

流程：
1. 从消息中提取微博链接并调用 extractor 解析。
2. 下载微博图片或视频，图片默认按原图优先 URL 下载。
3. 图文微博始终合并转发，视频微博按配置决定是否合并转发。
4. 合并转发中的视频节点会先注册为 callback file URL，兼容异机 NapCat。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video

from ..common import SizeLimitExceeded, get_weibo_image_path, get_weibo_video_path
from . import (
    WEIBO_DOWNLOAD_HEADERS,
    WeiboParseError,
    WeiboResult,
    extract_weibo_links,
)


class WeiboMixin:
    def _build_weibo_path(self, url: str, is_video: bool, request_id: str) -> Path:
        base_dir = get_weibo_video_path() if is_video else get_weibo_image_path()
        default_suffix = ".mp4" if is_video else ".jpg"
        suffix = (
            default_suffix
            if is_video
            else self._guess_media_suffix(url, default_suffix)
        )
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    async def _download_weibo_video(
        self, url: str, request_id: str, *, suffix: str = ".mp4"
    ) -> Path:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        video_headers = {
            **WEIBO_DOWNLOAD_HEADERS,
            "Accept": "video/*,application/octet-stream,*/*;q=0.8",
        }
        size_mb = await self._estimate_total_size_mb(
            url, None, headers=video_headers
        )
        logger.debug(
            "🐦 估算微博视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")

        output_path = self._build_weibo_path(url, is_video=True, request_id=request_id)
        if suffix != output_path.suffix:
            output_path = output_path.with_suffix(suffix)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=max_bytes,
            headers=video_headers,
        )
        return output_path

    async def _convert_weibo_live_photo_to_gif(self, video_path: Path) -> Path:
        """Convert a downloaded Weibo Live Photo movie to a looping GIF."""
        output_path = video_path.with_suffix(".gif")
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

    async def _convert_weibo_live_photo_to_mp4(self, video_path: Path) -> Path:
        """Transcode a Weibo Live Photo clip to a broadly compatible MP4."""
        output_path = video_path.with_suffix(".mp4")
        temp_path = output_path.with_name(f"{output_path.stem}.part.mp4")
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
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
                raise RuntimeError(details or "FFmpeg MP4 转码失败")
            if not temp_path.exists() or temp_path.stat().st_size == 0:
                raise RuntimeError("FFmpeg 未生成有效的 MP4 文件")
            await asyncio.to_thread(temp_path.replace, output_path)
            return output_path
        except BaseException:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            raise

    async def _download_weibo_image(self, url: str, request_id: str) -> Path:
        output_path = self._build_weibo_path(url, is_video=False, request_id=request_id)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=None,
            headers=WEIBO_DOWNLOAD_HEADERS,
        )
        return output_path

    def _build_weibo_summary(self, result: WeiboResult) -> str:
        lines: list[str] = []
        header = []
        if result.author:
            header.append(f"微博 @{result.author}")
        else:
            header.append("微博")
        if result.created_at:
            header.append(result.created_at)
        lines.append(" | ".join(header))

        if result.text:
            text = result.text.strip()
            if len(text) > 1200:
                text = text[:1197] + "..."
            lines.append(text)

        if result.source_url:
            lines.append(f"链接: {result.source_url}")
        return "\n".join(line for line in lines if line)

    async def _process_weibo(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        process_start = time.perf_counter()
        timing: dict[str, float] = {}

        self._refresh_config()
        if not self.weibo_enabled:
            return

        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]
        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()
        if not target_link:
            logger.warning("⚠️ 微博链接为空%s", source_tag)
            return
        logger.info("🐦 微博解析%s: %s", source_tag, target_link)

        parse_start = time.perf_counter()
        retry_count = getattr(self, "retry_count", 3)
        result: WeiboResult | None = None
        last_error = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.weibo_extractor.parse(target_link),
                    timeout=30.0,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ 微博解析任务已中断%s", source_tag)
                return
            except WeiboParseError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 微博解析失败%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 微博解析失败%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )
            except Exception as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 微博解析异常%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 微博解析异常%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )

        timing["parse"] = time.perf_counter() - parse_start
        if result is None:
            logger.error(
                "❌ 微博解析最终失败%s: %s, 解析耗时=%.2fs",
                source_tag,
                last_error,
                timing["parse"],
            )
            return

        logger.debug(
            "🐦 微博解析完成%s: 视频=%s, 图片=%d, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            timing["parse"],
        )

        if not result.video_url and not result.image_urls:
            logger.warning("⚠️ 微博未找到可下载媒体%s", source_tag)
            return

        summary_text = self._build_weibo_summary(result)
        media_components: list[object] = []
        chat_record_components: list[object] = []
        standalone_video_components: list[object] = []
        media_paths: list[Path] = []
        intermediate_paths: list[Path] = []
        image_paths_by_index: dict[int, Path] = {}
        failed_images = 0

        download_start = time.perf_counter()
        if result.video_url:
            try:
                video_path = await self._download_weibo_video(
                    result.video_url, request_id
                )
                media_paths.append(video_path)
                component = Video.fromFileSystem(str(video_path.resolve()))
                media_components.append(component)
                standalone_video_components.append(component)
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ 微博视频超过大小限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                return
            except Exception as exc:
                logger.error("❌ 微博视频下载失败%s: %s", source_tag, str(exc))
                return
        else:
            image_urls = result.image_urls[: self.weibo_max_media]
            for i, url in enumerate(image_urls):
                try:
                    image_path = await self._download_weibo_image(url, request_id)
                    media_paths.append(image_path)
                    image_paths_by_index[i] = image_path
                    component = Image.fromFileSystem(str(image_path.resolve()))
                    media_components.append(component)
                    chat_record_components.append(component)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_images += 1
                    logger.warning(
                        "⚠️ 微博图片下载失败%s [%d/%d]: %s",
                        source_tag,
                        i + 1,
                        len(image_urls),
                        str(exc),
                    )

            if getattr(self, "weibo_enable_live_photo", False) and image_paths_by_index:
                live_photo_video_urls = getattr(result, "live_photo_video_urls", [])
                if not isinstance(live_photo_video_urls, list):
                    live_photo_video_urls = []
                live_format = getattr(self, "weibo_live_photo_format", "视频")
                for image_index in sorted(image_paths_by_index):
                    try:
                        if image_index >= len(live_photo_video_urls):
                            continue
                        live_video_url = live_photo_video_urls[image_index]
                        if not isinstance(live_video_url, str) or not live_video_url:
                            continue
                        live_request_id = f"{request_id}_live_{image_index}"
                        live_video_path = await self._download_weibo_video(
                            live_video_url,
                            live_request_id,
                            suffix=".source",
                        )
                        intermediate_paths.append(live_video_path)
                        if live_format == "GIF":
                            live_media_path = await self._convert_weibo_live_photo_to_gif(
                                live_video_path
                            )
                            media_paths.append(live_media_path)
                            component = Image.fromFileSystem(
                                str(live_media_path.resolve())
                            )
                            media_components.append(component)
                            chat_record_components.append(component)
                        else:
                            live_media_path = await self._convert_weibo_live_photo_to_mp4(
                                live_video_path
                            )
                            media_paths.append(live_media_path)
                            component = Video.fromFileSystem(
                                str(live_media_path.resolve())
                            )
                            media_components.append(component)
                            standalone_video_components.append(component)
                    except asyncio.CancelledError:
                        raise
                    except SizeLimitExceeded:
                        logger.warning(
                            "⚠️ 微博 Live 图动态视频超过大小限制%s (图片%d/%d)，保留静态原图",
                            source_tag,
                            image_index + 1,
                            len(result.image_urls),
                        )
                    except Exception as exc:
                        logger.warning(
                            "⚠️ 微博 Live 图动态下载/转换失败%s (图片%d/%d)，保留静态原图: %s",
                            source_tag,
                            image_index + 1,
                            len(result.image_urls),
                            str(exc),
                        )

        timing["download"] = time.perf_counter() - download_start
        if not media_components:
            logger.warning(
                "⚠️ 微博媒体下载全部失败%s, 下载耗时=%.2fs",
                source_tag,
                timing["download"],
            )
            return

        is_image_post = bool(result.image_urls and not result.video_url)
        enable_merge_send = is_image_post or self.weibo_merge_send
        has_video = bool(standalone_video_components)
        should_send_chat_record = enable_merge_send and (
            bool(summary_text) or bool(chat_record_components)
        )

        try:
            send_start = time.perf_counter()
            if should_send_chat_record:
                nodes = Nodes([])
                sender_uin = self._get_merge_sender_uin(event)
                if summary_text:
                    nodes.nodes.append(
                        Node(uin=sender_uin, content=[Plain(summary_text)])
                    )
                for component in chat_record_components:
                    merge_component = await self._prepare_component_for_merge_send(
                        component
                    )
                    nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
                await event.send(MessageChain([nodes]))
                if has_video:
                    await asyncio.sleep(1.0)

            if has_video:
                for component in standalone_video_components:
                    await event.send(MessageChain([component]))
                    if component is not standalone_video_components[-1]:
                        await asyncio.sleep(1.0)
            elif not should_send_chat_record:
                await event.send(MessageChain([media_components[0]]))

            timing["send"] = time.perf_counter() - send_start

            total_elapsed = time.perf_counter() - process_start
            logger.info(
                "🐦 微博处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 发送=%.2fs, 总计=%.2fs",
                source_tag,
                (result.title or "未知标题")[:20],
                len(media_components),
                failed_images,
                timing.get("parse", 0),
                timing.get("download", 0),
                timing.get("send", 0),
                total_elapsed,
            )
        finally:
            cleanup_paths = [*media_paths, *intermediate_paths]
            if cleanup_paths:
                await self.cleanup_files(cleanup_paths, [])

    async def handle_weibo(self, event: AstrMessageEvent) -> None:
        if not self.weibo_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_weibo_links(event.message_str)
        logger.info("🐦 微博匹配链接: %s", links)
        if not links:
            return
        try:
            await self._process_weibo(event, links[0], is_from_card=False)
        except asyncio.CancelledError:
            logger.info("♻️ 微博解析任务已中断")
            return
