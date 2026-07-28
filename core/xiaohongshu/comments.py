from __future__ import annotations

import asyncio
import html
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps
from astrbot.api import logger

COMMENT_MODE_WEB = "网页截图"
COMMENT_MODE_DRAW = "自绘评论图"
XHS_COMMENT_MODES = (COMMENT_MODE_WEB, COMMENT_MODE_DRAW)

_XHS_COOKIE_DOMAIN = "xiaohongshu.com"
_COMMENT_SELECTORS = (
    ".comments-el .parent-comment",
    ".comment-list .parent-comment",
    ".comment-list .comment-item",
    ".comments-el .comment-item",
    "[class*='parent-comment']",
    "[class*='comment-item']",
)
_MAX_SCROLL_ROUNDS_LIMITED = 28
_MAX_SCROLL_ROUNDS_UNLIMITED = 80
_MAX_COMPOSE_HEIGHT = 3600


@dataclass(slots=True)
class XhsCommentItem:
    text: str


def parse_xhs_cookies(raw: str) -> list[dict[str, Any]]:
    """Parse Netscape cookies.txt or Cookie header text for Playwright."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if "\t" in raw:
        cookies = _parse_netscape_cookies(raw)
    else:
        cookies = _parse_cookie_header(raw)
    return [cookie for cookie in cookies if _is_xhs_cookie(cookie)]


def _is_xhs_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lower().lstrip(".")
    return domain == _XHS_COOKIE_DOMAIN or domain.endswith("." + _XHS_COOKIE_DOMAIN)


def _parse_netscape_cookies(raw: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        if not name:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            expires_int = int(float(expires))
        except Exception:
            expires_int = 0
        if expires_int > 0:
            cookie["expires"] = expires_int
        cookies.append(cookie)
    return cookies


def _parse_cookie_header(raw: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".xiaohongshu.com",
                "path": "/",
                "secure": True,
            }
        )
    return cookies


class XiaohongshuCommentScreenshotter:
    def __init__(self, font_path: Path | None = None):
        self.font_path = font_path

    async def capture(
        self,
        *,
        source_url: str,
        note_id: str | None,
        output_dir: Path,
        request_id: str,
        max_comments: int = 20,
        max_replies_per_comment: int = 5,
        mode: str = COMMENT_MODE_WEB,
        cookies_text: str = "",
    ) -> list[Path]:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            logger.warning("⚠️ 小红书评论截图跳过: Playwright 不可用 (%s)", exc)
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        max_comments = max(0, int(max_comments))
        max_replies_per_comment = max(0, int(max_replies_per_comment))
        mode = mode if mode in XHS_COMMENT_MODES else COMMENT_MODE_WEB
        url = _normalize_xhs_url(source_url, note_id)
        cookies = parse_xhs_cookies(cookies_text)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport={"width": 430, "height": 900},
                    device_scale_factor=2,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                )
                try:
                    if cookies:
                        await context.add_cookies(cookies)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await self._dismiss_obstructions(page)
                    locator = await self._load_comment_locator(page, max_comments)
                    count = await locator.count()
                    if max_comments > 0:
                        count = min(count, max_comments)
                    if count <= 0:
                        logger.info("🍠 小红书评论截图: 未找到可见评论")
                        return []
                    await self._prepare_nested_replies(
                        page,
                        locator,
                        count,
                        max_replies_per_comment,
                    )
                    if mode == COMMENT_MODE_DRAW:
                        items = await self._extract_comment_items(locator, count)
                        return await asyncio.to_thread(
                            self._render_comment_items,
                            items,
                            output_dir,
                            request_id,
                        )
                    return await self._capture_web_comments(
                        locator, count, output_dir, request_id
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def _dismiss_obstructions(self, page) -> None:
        await page.evaluate(
            """
            () => {
              for (const selector of ['.login-container', '.mask', '.modal', '[class*=login]']) {
                document.querySelectorAll(selector).forEach((el) => {
                  const style = window.getComputedStyle(el);
                  if (style.position === 'fixed' || style.position === 'absolute') el.remove();
                });
              }
            }
            """
        )

    async def _load_comment_locator(self, page, max_comments: int):
        locator = await self._pick_comment_locator(page)
        previous_count = -1
        stagnant_rounds = 0
        max_rounds = _MAX_SCROLL_ROUNDS_UNLIMITED if max_comments == 0 else _MAX_SCROLL_ROUNDS_LIMITED
        for _ in range(max_rounds):
            count = await locator.count()
            if max_comments > 0 and count >= max_comments:
                break
            if count == previous_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            if stagnant_rounds >= 4 and count > 0:
                break
            previous_count = count
            await page.evaluate(
                """
                () => {
                  const el = document.querySelector('.comments-el, .comment-list, [class*=comment]');
                  if (el && el.scrollHeight > el.clientHeight) {
                    el.scrollTop = el.scrollHeight;
                  } else {
                    window.scrollBy(0, Math.max(700, window.innerHeight * 0.85));
                  }
                }
                """
            )
            await page.wait_for_timeout(700)
            locator = await self._pick_comment_locator(page)
        return locator

    async def _pick_comment_locator(self, page):
        await self._mark_top_level_comments(page)
        marked = page.locator("[data-xhs-top-comment='1']")
        if await marked.count() > 0:
            return marked
        for selector in _COMMENT_SELECTORS:
            locator = page.locator(selector)
            if await locator.count() > 0:
                return locator
        return page.locator(_COMMENT_SELECTORS[-1])

    async def _mark_top_level_comments(self, page) -> None:
        await page.evaluate(
            """
            () => {
              document
                .querySelectorAll('[data-xhs-top-comment]')
                .forEach((el) => el.removeAttribute('data-xhs-top-comment'));
              let candidates = Array.from(
                document.querySelectorAll(
                  '.comments-el .parent-comment, .comment-list .parent-comment, [class*=parent-comment]'
                )
              );
              if (!candidates.length) {
                const rawCandidates = Array.from(
                  document.querySelectorAll('.comments-el .comment-item, .comment-list .comment-item')
                );
                candidates = rawCandidates.filter((el) => {
                  const cls = String(el.className || '').toLowerCase();
                  if (cls.includes('reply') || cls.includes('sub-comment') || cls.includes('child-comment')) {
                    return false;
                  }
                  return !rawCandidates.some((other) => other !== el && other.contains(el));
                });
              }
              candidates.forEach((el) => el.setAttribute('data-xhs-top-comment', '1'));
            }
            """
        )

    async def _prepare_nested_replies(
        self,
        page,
        locator,
        count: int,
        max_replies_per_comment: int,
    ) -> None:
        for index in range(count):
            item = locator.nth(index)
            await item.scroll_into_view_if_needed(timeout=5000)
            handle = await item.element_handle(timeout=5000)
            if handle is None:
                continue
            for _ in range(8):
                clicked = await page.evaluate(
                    """
                    (root) => {
                      const clickable = Array.from(
                        root.querySelectorAll('button, a, span, div')
                      ).find((el) => {
                        const text = (el.innerText || el.textContent || '').trim();
                        if (!text) return false;
                        if (!/(展开|更多|查看|回复)/.test(text)) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                      });
                      if (!clickable) return false;
                      clickable.click();
                      return true;
                    }
                    """,
                    handle,
                )
                if not clicked:
                    break
                await page.wait_for_timeout(450)
            await handle.evaluate(
                """
                (root, maxReplies) => {
                  const candidates = Array.from(
                    root.querySelectorAll(
                      '[class*=reply], [class*=sub-comment], [class*=child-comment], [class*=comment-item]'
                    )
                  ).filter((el) => {
                    if (el === root) return false;
                    const cls = String(el.className || '').toLowerCase();
                    return (
                      cls.includes('reply') ||
                      cls.includes('sub-comment') ||
                      cls.includes('child-comment')
                    );
                  });
                  const leafReplies = candidates.filter((el) => {
                    const nested = candidates.some((other) => other !== el && el.contains(other));
                    const text = (el.innerText || el.textContent || '').trim();
                    return !nested && text;
                  });
                  leafReplies.forEach((el, i) => {
                    if (maxReplies > 0 && i >= maxReplies) {
                      el.setAttribute('data-xhs-hidden-reply', '1');
                      el.style.display = 'none';
                    }
                  });
                }
                """,
                max_replies_per_comment,
            )

    async def _capture_web_comments(
        self,
        locator,
        count: int,
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            fragments: list[Path] = []
            for index in range(count):
                path = temp_root / f"comment_{index:03d}.png"
                item = locator.nth(index)
                await item.scroll_into_view_if_needed(timeout=5000)
                await item.screenshot(path=str(path), timeout=10000)
                fragments.append(path)
            return await asyncio.to_thread(
                self._compose_fragments,
                fragments,
                output_dir,
                request_id,
            )

    async def _extract_comment_items(self, locator, count: int) -> list[XhsCommentItem]:
        items: list[XhsCommentItem] = []
        for index in range(count):
            text = await locator.nth(index).inner_text(timeout=5000)
            text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            if text:
                items.append(XhsCommentItem(html.unescape(text)))
        return items

    def _compose_fragments(
        self,
        fragments: list[Path],
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        images = [Image.open(path).convert("RGB") for path in fragments if path.exists()]
        try:
            return _compose_images(images, output_dir, request_id, "web")
        finally:
            for image in images:
                image.close()

    def _render_comment_items(
        self,
        items: list[XhsCommentItem],
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        font = _load_font(self.font_path, 28)
        small_font = _load_font(self.font_path, 22)
        rendered: list[Image.Image] = []
        try:
            for item in items:
                rendered.append(_render_comment_card(item.text, font, small_font))
            return _compose_images(rendered, output_dir, request_id, "draw")
        finally:
            for image in rendered:
                image.close()


def _normalize_xhs_url(source_url: str, note_id: str | None) -> str:
    if note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    parsed = urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        return parsed._replace(query="", fragment="").geturl()
    return source_url


def _compose_images(
    images: list[Image.Image],
    output_dir: Path,
    request_id: str,
    suffix: str,
) -> list[Path]:
    paths: list[Path] = []
    chunk: list[Image.Image] = []
    chunk_height = 0
    page_index = 1
    for image in images:
        normalized = ImageOps.expand(image, border=(0, 0, 0, 12), fill=(255, 255, 255))
        if chunk and chunk_height + normalized.height > _MAX_COMPOSE_HEIGHT:
            paths.append(_save_chunk(chunk, output_dir, request_id, suffix, page_index))
            page_index += 1
            chunk = []
            chunk_height = 0
        chunk.append(normalized)
        chunk_height += normalized.height
    if chunk:
        paths.append(_save_chunk(chunk, output_dir, request_id, suffix, page_index))
    return paths


def _save_chunk(
    chunk: list[Image.Image],
    output_dir: Path,
    request_id: str,
    suffix: str,
    page_index: int,
) -> Path:
    width = max(image.width for image in chunk)
    height = sum(image.height for image in chunk)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for image in chunk:
        canvas.paste(image, ((width - image.width) // 2, y))
        y += image.height
    path = output_dir / f"{request_id}_comments_{suffix}_{page_index:02d}.png"
    canvas.save(path, format="PNG")
    canvas.close()
    return path


def _load_font(font_path: Path | None, size: int) -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _render_comment_card(
    text: str,
    font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> Image.Image:
    width = 860
    padding = 34
    lines = _wrap_text(text, font, width - padding * 2, max_lines=24)
    line_height = max(36, math.ceil(font.getbbox("口")[3] * 1.45))
    height = padding * 2 + line_height * len(lines) + 18
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (12, 12, width - 12, height - 12),
        radius=18,
        fill=(255, 255, 255),
        outline=(232, 232, 232),
        width=2,
    )
    y = padding
    for i, line in enumerate(lines):
        draw.text((padding, y), line, fill=(32, 32, 32), font=font if i else small_font)
        y += line_height
    return image


def _wrap_text(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        current = ""
        for char in paragraph:
            candidate = current + char
            if font.getlength(candidate) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = char
            if len(lines) >= max_lines:
                lines[-1] = lines[-1].rstrip() + "..."
                return lines
        if current:
            lines.append(current)
        if len(lines) >= max_lines:
            lines[-1] = lines[-1].rstrip() + "..."
            return lines
    return lines or ["暂无可见评论文本"]


__all__ = [
    "COMMENT_MODE_DRAW",
    "COMMENT_MODE_WEB",
    "XHS_COMMENT_MODES",
    "XhsCommentItem",
    "XiaohongshuCommentScreenshotter",
    "parse_xhs_cookies",
]
