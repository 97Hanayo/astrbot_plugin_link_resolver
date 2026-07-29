from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain

from ..common import get_nga_image_path, get_nga_screenshot_path
from .screenshot import extract_nga_links

NGA_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class NgaMixin:
    def _build_nga_image_path(self, url: str, request_id: str) -> Path:
        suffix = self._guess_media_suffix(url, ".jpg")
        return get_nga_image_path() / f"{self._hash_url(url)}_{request_id}{suffix}"

    async def _download_nga_image(self, url: str, request_id: str, referer: str) -> Path:
        output_path = self._build_nga_image_path(url, request_id)
        headers = {**NGA_IMAGE_HEADERS, "Referer": referer}
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=None,
            headers=headers,
        )
        return output_path

    async def _process_nga(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        self._refresh_config()
        if not getattr(self, "nga_enabled", False):
            return

        source_tag = "(from card)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]
        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()
        if not target_link:
            logger.warning("NGA link is empty%s", source_tag)
            return

        screenshotter = getattr(self, "nga_screenshotter", None)
        if screenshotter is None:
            logger.warning("NGA screenshotter is not initialized%s", source_tag)
            return

        logger.info("NGA parse%s: %s", source_tag, target_link)
        process_start = time.perf_counter()
        paths: list[Path] = []
        attachment_paths: list[Path] = []
        try:
            capture_result = await asyncio.wait_for(
                screenshotter.capture(
                    source_url=target_link,
                    output_dir=get_nga_screenshot_path(),
                    request_id=request_id,
                    cookies_text=getattr(self, "nga_cookies", ""),
                ),
                timeout=110.0,
            )
            paths = list(capture_result.screenshots)
            if not paths:
                logger.warning("NGA screenshot failed or returned no image%s", source_tag)
                return

            media_components: list[object] = [
                Image.fromFileSystem(str(path.resolve())) for path in paths
            ]
            image_urls = capture_result.image_urls[
                : max(0, int(getattr(self, "nga_max_attachment_images", 9)))
            ]
            failed_attachments = 0
            for url in image_urls:
                try:
                    attachment_path = await self._download_nga_image(
                        url, request_id, target_link
                    )
                    attachment_paths.append(attachment_path)
                    media_components.append(
                        Image.fromFileSystem(str(attachment_path.resolve()))
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_attachments += 1
                    logger.warning("NGA attachment image download failed%s: %s", source_tag, str(exc))

            if getattr(self, "nga_merge_send", False):
                sender_uin = self._get_merge_sender_uin(event)
                nodes = Nodes([])
                nodes.nodes.append(
                    Node(
                        uin=sender_uin,
                        content=[Plain(f"NGA\n{target_link}")],
                    )
                )
                for component in media_components:
                    merge_component = await self._prepare_component_for_merge_send(
                        component
                    )
                    nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
                await event.send(MessageChain([nodes]))
            else:
                await event.send(MessageChain(media_components))

            logger.info(
                "NGA done%s: screenshots=%d, attachments=%d, failed_attachments=%d, elapsed=%.2fs",
                source_tag,
                len(paths),
                len(attachment_paths),
                failed_attachments,
                time.perf_counter() - process_start,
            )
        except asyncio.CancelledError:
            logger.info("NGA parse task cancelled%s", source_tag)
            return
        except Exception as exc:
            logger.error("NGA parse failed%s: %s", source_tag, str(exc))
            await self._maybe_send_error(event, f"NGA解析失败: {exc}", source_tag)
        finally:
            cleanup_targets = paths + attachment_paths
            if cleanup_targets:
                await self.cleanup_files(cleanup_targets, [])

    async def handle_nga(self, event: AstrMessageEvent) -> None:
        if not getattr(self, "nga_enabled", False):
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_nga_links(event.message_str)
        logger.info("NGA matched links: %s", links)
        if not links:
            return
        await self._process_nga(event, links[0], is_from_card=False)
