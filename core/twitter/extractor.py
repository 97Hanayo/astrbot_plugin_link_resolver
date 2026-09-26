"""X/Twitter 内容提取器。

流程：
1. 从文本中识别 twitter.com / x.com 状态链接并按 tweet id 去重。
2. 调用 fxtwitter API 获取推文正文、作者、发布时间和媒体信息。
3. 将返回结构映射为插件内部统一结果，保留图片和视频直链列表。
4. 仅处理包含可下载图片或视频的推文，纯文本推文直接报错跳过。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from astrbot.api import logger

from ..common import (
    atomic_write_cookie_text,
    merge_cookie_updates,
    parse_set_cookie_updates,
    serialize_cookie_header,
)

TWITTER_REQUEST_TIMEOUT_SEC = 20.0
TWITTER_COOKIE_DOMAINS = ("x.com", "twitter.com")
TWITTER_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://x.com/",
}
TWITTER_DOWNLOAD_HEADERS = {
    **TWITTER_API_HEADERS,
    "Accept": "*/*",
}

TWITTER_STATUS_PATTERN = (
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/"
    r"(?:[A-Za-z0-9_]{1,32}|i/web)/status/(?P<tid>\d+)"
)
TWITTER_MESSAGE_PATTERN = rf"(?s).*(?:{TWITTER_STATUS_PATTERN})"

_STATUS_RE = re.compile(TWITTER_STATUS_PATTERN, re.IGNORECASE)


@dataclass(slots=True)
class TwitterResult:
    text: str | None
    author: str | None
    created_at: str | None
    image_urls: list[str]
    video_urls: list[str]
    source_url: str
    tweet_id: str | None = None


class TwitterParseError(RuntimeError):
    pass


class TwitterRetryableError(TwitterParseError):
    pass


def _normalize_url(url: str) -> str:
    raw = (url or "").strip().rstrip(")],.!?;")
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    match = _STATUS_RE.search(raw)
    if not match:
        return raw

    parsed = urlparse(raw)
    host = parsed.netloc or "x.com"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{path}"


def extract_twitter_links(text: str) -> list[str]:
    links: list[str] = []
    seen_ids: set[str] = set()
    if not text:
        return links

    for match in _STATUS_RE.finditer(text):
        tweet_id = match.group("tid")
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        links.append(_normalize_url(match.group(0)))
    return links


class TwitterExtractor:
    def __init__(self, timeout: float = TWITTER_REQUEST_TIMEOUT_SEC):
        self.timeout = timeout
        self.cookie = ""
        self._cookies: dict[str, str] = {}
        self._cookie_storage_path: Path | None = None
        self._cookie_update_callback: Callable[[str], None] | None = None
        self._auto_refresh_cookies = True
        self._cookie_refresh_interval_sec = 12 * 60 * 60
        self._last_cookie_refresh_at = 0.0

    def set_cookie(self, cookie: str | None) -> None:
        self.cookie = (cookie or "").strip()
        self._cookies = self._parse_cookie_header(self.cookie)
        self._last_cookie_refresh_at = 0.0

    def get_cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def has_cookie(self) -> bool:
        return bool(self._cookies)

    def set_cookie_storage(
        self,
        path: str | Path | None,
        on_update: Callable[[str], None] | None = None,
    ) -> None:
        self._cookie_storage_path = Path(path) if path else None
        self._cookie_update_callback = on_update

    def configure_cookie_refresh(
        self, enabled: bool = True, interval_hours: int | float = 12
    ) -> None:
        self._auto_refresh_cookies = bool(enabled)
        try:
            interval_hours = float(interval_hours)
        except (TypeError, ValueError):
            interval_hours = 12
        self._cookie_refresh_interval_sec = max(1.0, interval_hours * 3600)
        self._last_cookie_refresh_at = 0.0

    async def _refresh_user_cookies(self) -> None:
        if not self._auto_refresh_cookies or not self._cookies:
            return
        now = time.monotonic()
        if now - self._last_cookie_refresh_at < self._cookie_refresh_interval_sec:
            return
        self._last_cookie_refresh_at = now
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers=TWITTER_API_HEADERS,
                cookies=self._cookies,
                follow_redirects=True,
            ) as client:
                response = await client.get("https://x.com/")
            self._capture_cookie_updates(response)
        except Exception as exc:
            logger.debug("X Cookie 保活请求失败（不影响当前解析）: %s", exc)

    def _capture_cookie_updates(self, response: httpx.Response) -> None:
        if not self._auto_refresh_cookies or not self._cookies:
            return
        host = (response.url.host or "").lower().rstrip(".")
        updates = parse_set_cookie_updates(
            response.headers,
            response_host=host,
            allowed_hosts=TWITTER_COOKIE_DOMAINS,
        )
        if not updates or not merge_cookie_updates(self._cookies, updates):
            return
        self.cookie = serialize_cookie_header(self._cookies)
        if self._cookie_storage_path is not None:
            try:
                atomic_write_cookie_text(self._cookie_storage_path, self.cookie)
                logger.info("X 服务端更新的 Cookie 已持久化: %s", self._cookie_storage_path)
            except Exception as exc:
                logger.warning("持久化 X 更新 Cookie 失败: %s", exc)
        if self._cookie_update_callback is not None:
            try:
                self._cookie_update_callback(self.cookie)
            except Exception as exc:
                logger.debug("同步 X Cookie 配置失败: %s", exc)

    @staticmethod
    def _parse_cookie_header(raw: str | None) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or (line.startswith("#") and not line.lower().startswith("#httponly_")):
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain = parts[0].lower().removeprefix("#httponly_").lstrip(".")
                    if not any(domain == item or domain.endswith("." + item) for item in TWITTER_COOKIE_DOMAINS):
                        continue
                    name, value = parts[5].strip(), parts[6].strip()
                    if name and value:
                        cookies[name] = value
                    continue
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            for part in line.split(";"):
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name, value = name.strip(), value.strip()
                if name and value:
                    cookies[name] = value
        return cookies

    async def parse(self, text_or_url: str) -> TwitterResult:
        await self._refresh_user_cookies()
        url = _normalize_url(text_or_url)
        tweet_id = self._extract_tweet_id(url)
        payload = await self._fetch_status_json(tweet_id)
        result = self._build_result(payload, url)
        if not result.image_urls and not result.video_urls:
            raise TwitterParseError("推文中未找到可下载媒体")
        return result

    def _extract_tweet_id(self, url: str) -> str:
        if match := _STATUS_RE.search(url):
            return match.group("tid")
        raise TwitterParseError(f"无法从 X 链接提取 tweet id: {url}")

    async def _fetch_status_json(self, tweet_id: str) -> dict[str, Any]:
        api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers=TWITTER_API_HEADERS,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(api_url)
                    if response.status_code >= 500:
                        raise TwitterRetryableError(
                            f"X 详情接口临时失败: {response.status_code}"
                        )
                    if response.status_code >= 400:
                        raise TwitterParseError(
                            f"X 详情接口失败: {response.status_code}"
                        )
                    payload = response.json()
            except TwitterRetryableError as exc:
                last_error = exc
            except asyncio.TimeoutError as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            except TwitterParseError:
                raise
            else:
                if isinstance(payload, dict):
                    return payload
                raise TwitterParseError("X 详情接口返回格式异常")

            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

        raise TwitterRetryableError(f"X 详情接口请求失败: {last_error or '未知错误'}")

    def _build_result(self, payload: dict[str, Any], source_url: str) -> TwitterResult:
        tweet = payload.get("tweet")
        if not isinstance(tweet, dict):
            tweet = payload.get("status")
        if not isinstance(tweet, dict):
            raise TwitterParseError("X 详情接口未返回推文数据")

        text = str(tweet.get("text") or "").strip() or None
        author = self._format_author(tweet.get("author"))
        created_at = self._format_created_at(tweet.get("created_at"))
        media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}

        image_urls: list[str] = []
        seen_images: set[str] = set()
        for photo in media.get("photos", []) or []:
            if not isinstance(photo, dict):
                continue
            url = str(photo.get("url") or "").strip()
            if url and url not in seen_images:
                seen_images.add(url)
                image_urls.append(url)

        video_urls: list[str] = []
        seen_videos: set[str] = set()
        for video in media.get("videos", []) or []:
            if not isinstance(video, dict):
                continue
            url = str(video.get("url") or "").strip()
            if url and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        for item in media.get("all", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if item_type == "photo" and url not in seen_images:
                seen_images.add(url)
                image_urls.append(url)
            elif item_type in {"video", "gif"} and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        external_media = media.get("external")
        if isinstance(external_media, dict):
            url = str(external_media.get("url") or "").strip()
            media_type = str(external_media.get("type") or "").strip().lower()
            if url and media_type in {"video", "gif"} and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        return TwitterResult(
            text=text,
            author=author,
            created_at=created_at,
            image_urls=image_urls,
            video_urls=video_urls,
            source_url=_normalize_url(source_url),
            tweet_id=self._extract_tweet_id(source_url),
        )

    @staticmethod
    def _format_author(author_info: Any) -> str | None:
        if not isinstance(author_info, dict):
            return None
        name = str(author_info.get("name") or "").strip()
        screen_name = str(author_info.get("screen_name") or "").strip()
        if name and screen_name:
            return f"{name}(@{screen_name})"
        if name:
            return name
        if screen_name:
            return screen_name
        return None

    @staticmethod
    def _format_created_at(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return text
        return dt.strftime("%Y-%m-%d")
