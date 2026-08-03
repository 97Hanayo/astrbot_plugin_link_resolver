from __future__ import annotations

import asyncio
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..common.image_compression import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_IMAGE_HEIGHT,
    fit_image_file_limits,
)
from ..common.playwright_manager import (
    browser_channel_candidates,
    configure_playwright_browser_path,
    launch_chromium,
)

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

NGA_MESSAGE_PATTERN = (
    r"(?:https?://)?(?:(?:bbs|nga)\.nga\.cn|nga\.178\.com|ngabbs\.com|m\.ngabbs\.com)"
    r"/(?:read|thread)\.php\?[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*(?:tid|pid)=\d+"
    r"[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*"
)

_NGA_COOKIE_DOMAINS = ("nga.cn", "178.com", "ngabbs.com")
_MAX_SCREENSHOT_BYTES = DEFAULT_MAX_IMAGE_BYTES
_MAX_SCREENSHOT_HEIGHT = DEFAULT_MAX_IMAGE_HEIGHT


@dataclass(slots=True)
class NgaCaptureResult:
    screenshots: list[Path]
    image_urls: list[str]


def extract_nga_links(text: str) -> list[str]:
    links = re.findall(NGA_MESSAGE_PATTERN, text or "", flags=re.IGNORECASE)
    normalized: list[str] = []
    for link in links:
        if not link.startswith(("http://", "https://")):
            link = "https://" + link
        normalized.append(_normalize_nga_url(link))
    return list(dict.fromkeys(normalized))


def parse_nga_cookies(raw: str) -> list[dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    cookies = _parse_netscape_cookies(raw) if "\t" in raw else _parse_cookie_header(raw)
    return _expand_nga_cookie_domains(
        [cookie for cookie in cookies if _is_nga_cookie(cookie)]
    )


def _normalize_nga_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "bbs.nga.cn"
    path = parsed.path or "/read.php"
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "tid" in query:
        query = {"tid": [query["tid"][0]]}
    elif "pid" in query:
        query = {"pid": [query["pid"][0]]}
    compact_query = urlencode(query, doseq=True)
    return urlunparse((scheme, netloc, path, "", compact_query, ""))


def _is_nga_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lower().lstrip(".")
    return any(domain == item or domain.endswith("." + item) for item in _NGA_COOKIE_DOMAINS)


def _expand_nga_cookie_domains(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        source_domain = str(cookie.get("domain") or "").lower().lstrip(".")
        candidate_domains = [source_domain]
        if source_domain in {"ngabbs.com", "m.ngabbs.com"}:
            candidate_domains.extend(["nga.cn", "bbs.nga.cn", "nga.178.com"])
        elif source_domain in {"nga.cn", "bbs.nga.cn"}:
            candidate_domains.extend(["ngabbs.com", "nga.178.com"])
        elif source_domain in {"178.com", "nga.178.com"}:
            candidate_domains.extend(["nga.cn", "bbs.nga.cn", "ngabbs.com"])

        for domain in candidate_domains:
            if not domain:
                continue
            cloned = dict(cookie)
            cloned["domain"] = "." + domain.lstrip(".")
            key = (
                str(cloned.get("name") or ""),
                str(cloned.get("domain") or ""),
                str(cloned.get("path") or "/"),
            )
            if key in seen:
                continue
            seen.add(key)
            expanded.append(cloned)
    return expanded


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
            {"name": name, "value": value.strip(), "domain": ".nga.cn", "path": "/", "secure": True}
        )
    return cookies


class NgaScreenshotter:
    async def capture(
        self,
        *,
        source_url: str,
        output_dir: Path,
        request_id: str,
        cookies_text: str = "",
    ) -> NgaCaptureResult:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            logger.warning("NGA screenshot skipped: Playwright unavailable (%s)", exc)
            return NgaCaptureResult([], [])

        output_dir.mkdir(parents=True, exist_ok=True)
        url = _normalize_nga_url(source_url)
        cookies = parse_nga_cookies(cookies_text)

        configure_playwright_browser_path()
        async with async_playwright() as p:
            browser = await self._launch_browser(p)
            try:
                context = await browser.new_context(
                    viewport={"width": 980, "height": 1400},
                    device_scale_factor=1,
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
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await self._wait_for_page_images(page)
                    image_urls = await self._prepare_capture(page)
                    target = page.locator("#codex-nga-capture-root")
                    if await target.count() <= 0:
                        logger.warning("NGA screenshot skipped: capture root not found")
                        return NgaCaptureResult([], image_urls)
                    await self._wait_for_capture_images(page)
                    paths = await self._capture_sections(page, output_dir, request_id)
                    return NgaCaptureResult(paths, image_urls)
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def _launch_browser(self, playwright):
        return await launch_chromium(
            playwright,
            headless=True,
            fallback_executable_paths=browser_channel_candidates(),
        )

    async def _wait_for_page_images(self, page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:
            logger.debug("NGA networkidle wait skipped: %s", exc)
        try:
            await asyncio.wait_for(
                page.evaluate(
                    """
                    async () => {
                      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                      const documentElement = document.documentElement;
                      const maxY = Math.max(
                        document.body?.scrollHeight || 0,
                        documentElement?.scrollHeight || 0
                      );
                      for (let y = 0; y <= maxY; y += 900) {
                        window.scrollTo(0, y);
                        await sleep(220);
                      }
                      window.scrollTo(0, 0);
                    }
                    """,
                ),
                timeout=12,
            )
        except Exception as exc:
            logger.debug("NGA lazy image scroll wait skipped: %s", exc)

        await self._wait_for_images(page, "img")

    async def _wait_for_capture_images(self, page) -> None:
        await self._wait_for_images(page, "#codex-nga-capture-root img")

    async def _capture_sections(
        self,
        page,
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        sections = page.locator("#codex-nga-capture-root .codex-nga-capture-section")
        count = await sections.count()
        if count <= 0:
            sections = page.locator("#codex-nga-capture-root")
            count = await sections.count()

        paths: list[Path] = []
        for index in range(count):
            section = sections.nth(index)
            suffix = await section.get_attribute("data-codex-nga-section")
            suffix = _sanitize_filename_part(suffix or f"section_{index + 1:02d}")
            path = output_dir / f"{request_id}_nga_{index + 1:02d}_{suffix}.png"
            await section.screenshot(path=str(path), timeout=20000)
            path = await asyncio.to_thread(_fit_screenshot_limits, path)
            paths.append(path)
        return paths

    async def _wait_for_images(self, page, selector: str) -> None:
        try:
            await asyncio.wait_for(
                page.evaluate(
                    """
                    async (selector) => {
                      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                      const lazyKeys = [
                        'data-src',
                        'data-original',
                        'data-lazy-src',
                        'data-url',
                        'data-file',
                        '_src',
                        'file'
                      ];
                      const normalize = (raw) => {
                        if (!raw || raw.startsWith('data:') || raw.startsWith('javascript:')) return '';
                        try {
                          return new URL(raw, location.href).href;
                        } catch {
                          return '';
                        }
                      };
                      const hydrateLazySrc = (img) => {
                        img.loading = 'eager';
                        img.decoding = 'async';
                        const currentUrl = normalize(img.currentSrc || img.src || '');
                        let lazyUrl = '';
                        for (const key of lazyKeys) {
                          const value = img.getAttribute(key);
                          const url = normalize(value);
                          if (url) {
                            lazyUrl = url;
                            break;
                          }
                        }
                        if (
                          lazyUrl &&
                          (!currentUrl || currentUrl.includes('placeholder') || currentUrl.includes('/transparent'))
                        ) {
                          img.src = lazyUrl;
                          return;
                        }
                        if (currentUrl) return;
                        const srcset = img.getAttribute('data-srcset') || img.getAttribute('data-original-srcset');
                        if (srcset) img.setAttribute('srcset', srcset);
                      };
                      const waitOne = async (img) => {
                        hydrateLazySrc(img);
                        if (!img.currentSrc && !img.src) return;
                        if (img.complete && img.naturalWidth > 0) return;
                        await Promise.race([
                          new Promise((resolve) => {
                            img.addEventListener('load', resolve, { once: true });
                            img.addEventListener('error', resolve, { once: true });
                          }),
                          sleep(8000)
                        ]);
                        if (img.decode && img.complete && img.naturalWidth > 0) {
                          try {
                            await Promise.race([img.decode(), sleep(3000)]);
                          } catch {}
                        }
                      };
                      await Promise.allSettled(Array.from(document.querySelectorAll(selector)).map(waitOne));
                    }
                    """,
                    selector,
                ),
                timeout=18,
            )
        except Exception as exc:
            logger.debug("NGA image wait skipped for %s: %s", selector, exc)

    async def _prepare_capture(self, page) -> list[str]:
        image_urls = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 160 && rect.height > 60 &&
                  style.display !== 'none' && style.visibility !== 'hidden';
              };
              const compactText = (el) => (el.innerText || el.textContent || '')
                .replace(/\\s+/g, ' ')
                .trim();
              const isChrome = (el) => {
                const text = compactText(el);
                if (text.length > 8000) return true;
                return /登录|注册|发帖|回帖|版面列表|广告|客户端下载/.test(text) && text.length < 80;
              };
              const selectors = [
                '[id^="postcontainer"]',
                '[id^="post_"]',
                '.postrow',
                '.postbox',
                '.forumbox',
                'table[id*="post"]',
                'article'
              ];
              const postCandidates = [];
              for (const selector of selectors) {
                document.querySelectorAll(selector).forEach((el) => {
                  const text = compactText(el);
                  if (visible(el) && text.length > 80 && !isChrome(el)) postCandidates.push(el);
                });
                if (postCandidates.length) break;
              }
              const mainPost = postCandidates[0] || Array.from(document.body.children)
                .find((el) => visible(el) && compactText(el).length > 300);

              const hotPattern = /(热点回复|热门回复|热评|高赞回复|推荐回复|精彩回复)/;
              const hotNodes = [];
              document.querySelectorAll('section,div,table,tbody,tr').forEach((el) => {
                const text = compactText(el);
                if (!visible(el) || !hotPattern.test(text) || text.length < 20) return;
                if (mainPost && (el === mainPost || mainPost.contains(el))) return;
                if (hotNodes.some((node) => node.contains(el))) return;
                hotNodes.push(el);
              });

              document.querySelectorAll('.modal,.mask,.popup,[class*="login"],[class*="ad"],iframe')
                .forEach((el) => el.remove());

              const isAttachmentImage = (url, el) => {
                if (!url || url.startsWith('data:') || url === 'about:blank' || url.startsWith('javascript:')) return false;
                const lower = url.toLowerCase();
                const cls = String(el.className || '').toLowerCase();
                if (cls.includes('avatar') || cls.includes('smile') || lower.includes('/post/smile/')) return false;
                if (lower.includes('/attachments/')) return true;
                return /\\.(?:apng|avif|gif|jpe?g|png|webp)(?:[?#].*)?$/.test(lower);
              };
              const collectImages = (roots) => {
                const urls = [];
                const seen = new Set();
                const lazyKeys = [
                  'data-src',
                  'data-original',
                  'data-lazy-src',
                  'data-url',
                  'data-file',
                  '_src',
                  'file'
                ];
                roots.filter(Boolean).forEach((rootNode) => {
                  rootNode.querySelectorAll('img,a').forEach((el) => {
                    const rect = el.getBoundingClientRect();
                    const rawCandidates = [
                      el.currentSrc,
                      el.src,
                      el.href,
                      el.getAttribute('href'),
                      el.getAttribute('src'),
                      ...lazyKeys.map((key) => el.getAttribute(key))
                    ];
                    let url = '';
                    for (const raw of rawCandidates) {
                      try {
                        const candidate = new URL(raw || '', location.href).href;
                        if (isAttachmentImage(candidate, el)) {
                          url = candidate;
                          break;
                        }
                      } catch {}
                    }
                    if (!url) return;
                    if (rect.width > 0 && rect.height > 0 && (rect.width < 80 || rect.height < 80) && !url.includes('/attachments/')) return;
                    if (seen.has(url)) return;
                    seen.add(url);
                    urls.push(url);
                  });
                });
                return urls;
              };
              const attachmentUrls = collectImages([mainPost, ...hotNodes]);

              const root = document.createElement('div');
              root.id = 'codex-nga-capture-root';
              root.style.cssText = [
                'box-sizing:border-box',
                'width:940px',
                'padding:14px',
                'background:#f6f0df',
                'color:#2f2417',
                'font-family:Arial,"Microsoft YaHei",sans-serif',
                'font-size:14px',
                'line-height:1.55'
              ].join(';');

              const addClone = (el, label, key) => {
                if (!el) return;
                const box = document.createElement('div');
                box.className = 'codex-nga-capture-section';
                box.setAttribute('data-codex-nga-section', key || label);
                box.style.cssText = 'margin:0 0 12px 0;padding:10px;background:#fffaf0;border:1px solid #c8b88a;';
                const title = document.createElement('div');
                title.textContent = label;
                title.style.cssText = 'font-weight:700;margin:0 0 8px 0;color:#6b3f12;';
                const clone = el.cloneNode(true);
                clone.querySelectorAll('script,style,iframe,textarea,input,button,select,.reply,.quote,[class*="replyer"],[class*="fastpost"]')
                  .forEach((node) => node.remove());
                clone.style.maxWidth = '100%';
                box.appendChild(title);
                box.appendChild(clone);
                root.appendChild(box);
              };

              addClone(mainPost, 'NGA 主楼', 'main');
              hotNodes.slice(0, 2).forEach((node, index) => {
                addClone(
                  node,
                  index === 0 ? '热点回复' : '热点回复补充',
                  index === 0 ? 'hot_reply_1' : 'hot_reply_2'
                );
              });

              if (!root.children.length) {
                addClone(document.body, 'NGA 页面', 'page');
              }
              document.body.innerHTML = '';
              document.body.style.cssText = 'margin:0;background:#f6f0df;';
              document.body.appendChild(root);
              document.querySelectorAll('#codex-nga-capture-root img').forEach((img) => {
                img.style.maxWidth = '100%';
                img.style.height = 'auto';
              });
              return attachmentUrls;
            }
            """
        )
        if not isinstance(image_urls, list):
            return []
        return [str(url) for url in image_urls if isinstance(url, str)]


def _fit_screenshot_limits(
    path: Path,
    *,
    max_bytes: int = _MAX_SCREENSHOT_BYTES,
    max_height: int = _MAX_SCREENSHOT_HEIGHT,
) -> Path:
    return fit_image_file_limits(
        path,
        max_bytes=max_bytes,
        max_height=max_height,
        force_jpeg=True,
    )


def _sanitize_filename_part(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip().lower())
    normalized = normalized.strip("_")
    return normalized[:40] or "section"


__all__ = [
    "NGA_MESSAGE_PATTERN",
    "NgaCaptureResult",
    "NgaScreenshotter",
    "extract_nga_links",
    "parse_nga_cookies",
]
