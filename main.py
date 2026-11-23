# region 导入
import asyncio
import hashlib
import json
import re
import time
import traceback
from pathlib import Path
from random import choice
from urllib.parse import urlparse

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .core.bilibili import BILI_MESSAGE_PATTERN, BilibiliMixin
from .core.common import (
    SizeLimitExceeded,
    get_bili_cookies_file,
    get_douyin_cookies_file,
    get_generic_cookies_file,
    get_nga_cookies_file,
    get_twitter_cookies_file,
    get_weibo_cache,
    get_weibo_cookies_file,
    get_xhs_cookies_file,
    get_youtube_cookies_file,
)
from .core.common.qq_video_relay import (
    RelayVideoReferenceNode,
    send_components_to_private_relay,
    send_videos_to_private_relay,
)
from .core.common.card_renderer import find_default_font, find_emoji_font
from .core.common.font_manager import (
    get_managed_font_paths,
    get_user_font_paths,
    install_managed_fonts,
    set_managed_fonts_enabled,
    set_user_font_paths,
)
from .core.douyin import DOUYIN_MESSAGE_PATTERN, DouyinExtractor
from .core.douyin.handler import DouyinMixin
from .core.generic import GENERIC_URL_PATTERN, GenericExtractor
from .core.generic.handler import GenericMixin
from .core.registry import (
    PLATFORM_BY_KEY,
    PLATFORM_DEFAULT_LABELS,
    PLATFORM_LABELS,
    PLATFORMS,
    TASK_QUALNAME_TOKENS,
    classify_links,
)
from .core.nga import (
    NGA_MESSAGE_PATTERN,
    NgaMixin,
    NgaScreenshotter,
    parse_nga_cookies,
)
from .core.twitter import TWITTER_MESSAGE_PATTERN, TwitterExtractor
from .core.twitter.handler import TwitterMixin
from .core.weibo import (
    WEIBO_MESSAGE_PATTERN,
    WeiboExtractor,
    WeiboInteractiveLoginError,
    WeiboInteractiveLoginManager,
    WeiboLoginCancelledError,
    WeiboLoginCaptchaBlockedError,
    WeiboLoginTimeoutError,
)
from .core.weibo.handler import WeiboMixin
from .core.weibo.cookie_storage import load_cookie_source, record_refreshed_source
from .core.xiaohongshu import (
    COMMENT_MODE_WEB,
    XHS_COMMENT_MODES,
    XHS_MESSAGE_PATTERN,
    XHS_VIDEO_QUALITY_OPTIONS,
    XiaohongshuCardRenderer,
    XiaohongshuCommentScreenshotter,
    XiaohongshuExtractor,
)
from .core.xiaohongshu.handler import XiaohongshuMixin
from .core.youtube import YOUTUBE_MESSAGE_PATTERN, YoutubeExtractor
from .core.youtube.handler import YoutubeMixin

# endregion

# region 运行时常量
TASK_NAME_PREFIX = "link-resolver-parse"
# JSON 卡片分发时, 平台处理完毕的标记(由 _card_* 异步生成器在最后产出, 不会转发给框架)
CARD_HANDLED = object()
SUMMARY_MODE_TEXT = "文字摘要"
SUMMARY_MODE_CARD = "渲染卡片"
WEIBO_EXPIRE_NOTIFY_COOLDOWN_SEC = 6 * 3600
# Xiaoheihe share cards include API, image, and app-icon URLs that are not content links.
IGNORED_CARD_LINK_DOMAINS = (
    "xiaoheihe.cn",
    "xiaoheihe.com",
    "ugcimg.cn",
    "gtimg.cn",
)
# endregion


# region LinkResolver 类
@register(
    "astrbot_plugin_link_resolver",
    "acacia",
    "解析 & 下载 Bilibili/抖音/小红书/微博/X/NGA/YouTube/通用站点",
    "1.1.14",
)
class LinkResolverPlugin(
    BilibiliMixin,
    DouyinMixin,
    XiaohongshuMixin,
    WeiboMixin,
    TwitterMixin,
    NgaMixin,
    YoutubeMixin,
    GenericMixin,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        # 注意：必须在 _active_parse_tasks 初始化之前调用；
        # 该方法通过 asyncio.all_tasks() 扫描清理旧任务，不依赖实例任务池。
        self._cancel_previous_parse_tasks()
        self._active_parse_tasks: set[asyncio.Task] = set()
        self.douyin_extractor = DouyinExtractor()
        self.weibo_extractor = WeiboExtractor()
        self.weibo_login_manager = WeiboInteractiveLoginManager()
        self._weibo_keepalive_task: asyncio.Task | None = None
        self._weibo_login_task: asyncio.Task | None = None
        self._weibo_cookie_expired_notified_fingerprint: str | None = None
        self._weibo_cookie_last_notify_at: float = 0.0
        self.xhs_extractor = XiaohongshuExtractor()
        self.twitter_extractor = TwitterExtractor()
        self.font_auto_install_enabled = False
        self.youtube_extractor = YoutubeExtractor()
        self.generic_extractor = GenericExtractor()
        self.custom_primary_font_path: str | None = None
        self.custom_emoji_font_path: str | None = None
        self.user_primary_font_ready = False
        self.user_emoji_font_ready = False
        self.managed_primary_font_ready = False
        self.managed_emoji_font_ready = False
        self.xhs_renderer: XiaohongshuCardRenderer | None = None
        self.xhs_comment_screenshotter: XiaohongshuCommentScreenshotter | None = None
        self.nga_screenshotter: NgaScreenshotter | None = None
        self._refresh_config()

    async def initialize(self) -> None:
        """预加载提取器、启动微博保活并初始化字体。"""
        if self._weibo_keepalive_task is None or self._weibo_keepalive_task.done():
            self._weibo_keepalive_task = asyncio.create_task(
                self.weibo_extractor.cookie_keepalive_loop(),
                name="link-resolver-weibo-cookie-keepalive",
            )
        if self.generic_enabled:
            await asyncio.to_thread(self.generic_extractor.warm_up)
        if not self.font_auto_install_enabled:
            return

        managed_paths = await asyncio.to_thread(install_managed_fonts)
        self.managed_primary_font_ready = managed_paths.primary is not None
        self.managed_emoji_font_ready = managed_paths.emoji is not None
        self._refresh_config()

    async def terminate(self) -> None:
        if self._weibo_keepalive_task is not None:
            self._weibo_keepalive_task.cancel()
            try:
                await self._weibo_keepalive_task
            except asyncio.CancelledError:
                pass
            self._weibo_keepalive_task = None
        login_manager = getattr(self, "weibo_login_manager", None)
        if login_manager is not None:
            login_manager.cancel()
        login_task = getattr(self, "_weibo_login_task", None)
        if login_task is not None:
            login_task.cancel()
            try:
                await login_task
            except asyncio.CancelledError:
                pass
            self._weibo_login_task = None

    # region 配置
    def _get_config_value(self, key: str, default):
        keys = key.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def _read_summary_mode(self, key: str) -> str:
        mode = str(self._get_config_value(key, SUMMARY_MODE_TEXT)).strip()
        if mode not in (SUMMARY_MODE_TEXT, SUMMARY_MODE_CARD):
            return SUMMARY_MODE_TEXT
        return mode

    def _refresh_config(self) -> None:
        self._configure_managed_fonts()
        user_font_paths = get_user_font_paths()
        managed_font_paths = get_managed_font_paths()
        self.managed_primary_font_ready = managed_font_paths.primary is not None
        self.managed_emoji_font_ready = managed_font_paths.emoji is not None
        self.default_primary_font = find_default_font()
        self.default_emoji_font = find_emoji_font()
        self.user_primary_font_ready = bool(
            user_font_paths.primary
            and self.default_primary_font == user_font_paths.primary
        )
        self.user_emoji_font_ready = bool(
            user_font_paths.emoji and self.default_emoji_font == user_font_paths.emoji
        )
        if self.custom_primary_font_path and not self.user_primary_font_ready:
            logger.warning(
                "⚠️ 自定义主字体路径不可用或无法加载: %s",
                self.custom_primary_font_path,
            )
        if self.custom_emoji_font_path and not self.user_emoji_font_ready:
            logger.warning(
                "⚠️ 自定义 Emoji 字体路径不可用或无法加载: %s",
                self.custom_emoji_font_path,
            )

        # 平台启用列表
        enable_platforms = self._get_config_value(
            "enable_platforms", list(PLATFORM_DEFAULT_LABELS)
        )
        if not isinstance(enable_platforms, list):
            enable_platforms = list(PLATFORM_DEFAULT_LABELS)
        # 生成 bili_enabled / douyin_enabled / ... (属性名由注册表决定)
        for platform in PLATFORMS:
            setattr(self, platform.enabled_attr, platform.label in enable_platforms)

        # B站配置
        self.quality_label = str(
            self._get_config_value("bili_settings.video_quality", "1080P高帧率")
        )
        self.codecs_label = str(
            self._get_config_value("bili_settings.video_codecs", "AVC")
        )
        self.allow_hdr = bool(self._get_config_value("bili_settings.allow_hdr", False))
        self.allow_dolby = bool(
            self._get_config_value("bili_settings.allow_dolby", False)
        )
        self.bili_merge_send = bool(
            self._get_config_value("bili_settings.merge_send", False)
        )
        self.bili_summary_mode = self._read_summary_mode("bili_settings.summary_mode")
        self.bili_render_card = self.bili_summary_mode == SUMMARY_MODE_CARD
        self.enable_multi_page = bool(
            self._get_config_value("bili_settings.enable_multi_page", True)
        )
        self.multi_page_max = max(
            1, int(self._get_config_value("bili_settings.multi_page_max", 3))
        )
        self.bili_max_duration_seconds = max(
            0, int(self._get_config_value("bili_settings.max_duration_seconds", 300))
        )
        self.allow_quality_fallback = bool(
            self._get_config_value("bili_settings.allow_quality_fallback", True)
        )
        self.bili_auto_refresh_cookies = bool(
            self._get_config_value("bili_settings.auto_refresh_cookies", True)
        )
        self.bili_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(self._get_config_value("bili_settings.cookie_refresh_interval_hours", 12)),
            ),
        )
        bili_cookies_str = str(
            self._get_config_value("bili_settings.cookies", "")
        ).strip()
        bili_cookies_file = get_bili_cookies_file()
        persisted_bili_cookies = ""
        if bili_cookies_file.exists():
            try:
                persisted_bili_cookies = bili_cookies_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("⚠️ 读取 B站 Cookie 文件失败: %s", str(exc))
        if persisted_bili_cookies and self._load_cookies_from_file(bili_cookies_file):
            bili_cookies_str = persisted_bili_cookies
            logger.info("🍪 使用已持久化的 B站 Cookie: %s", bili_cookies_file)
        elif bili_cookies_str:
            try:
                bili_cookies_file.parent.mkdir(parents=True, exist_ok=True)
                if "\n" not in bili_cookies_str and ".bilibili.com" in bili_cookies_str:
                    bili_cookies_str = re.sub(
                        r"\s+(\.(?:www\.)?bilibili\.com\s)",
                        r"\n\1",
                        bili_cookies_str,
                    )
                    bili_cookies_str = bili_cookies_str.replace("# ", "\n# ").strip()
                bili_cookies_file.write_text(bili_cookies_str, encoding="utf-8")
                logger.info("🍪 B站 Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 B站 Cookie 文件失败: %s", str(exc))
        self.bili_cookie_storage_path = bili_cookies_file

        # 抖音配置
        self.douyin_max_media = max(
            1, int(self._get_config_value("douyin_settings.max_media", 99))
        )
        self.douyin_merge_send = bool(
            self._get_config_value("douyin_settings.merge_send", False)
        )
        self.douyin_merge_forward_chunk_size = max(
            1,
            min(
                100,
                int(
                    self._get_config_value("douyin_settings.merge_forward_chunk_size", 20)
                ),
            ),
        )
        self.douyin_enable_live_photo = bool(
            self._get_config_value("douyin_settings.enable_live_photo", False)
        )
        self.douyin_live_photo_video_limit = max(
            0, int(self._get_config_value("douyin_settings.live_photo_video_limit", 1))
        )
        _douyin_live_photo_format = str(
            self._get_config_value("douyin_settings.live_photo_format", "视频")
        ).strip()
        self.douyin_live_photo_format = (
            _douyin_live_photo_format
            if _douyin_live_photo_format in ("视频", "GIF")
            else "视频"
        )
        self.douyin_summary_mode = self._read_summary_mode(
            "douyin_settings.summary_mode"
        )
        self.douyin_render_card = self.douyin_summary_mode == SUMMARY_MODE_CARD
        self.douyin_auto_refresh_cookies = bool(
            self._get_config_value("douyin_settings.auto_refresh_cookies", True)
        )
        self.douyin_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(self._get_config_value("douyin_settings.cookie_refresh_interval_hours", 12)),
            ),
        )
        douyin_cookies_str = str(
            self._get_config_value("douyin_settings.cookies", "")
        ).strip()
        douyin_cookies_file = get_douyin_cookies_file()
        persisted_douyin_cookies = ""
        if douyin_cookies_file.exists():
            try:
                persisted_douyin_cookies = douyin_cookies_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("⚠️ 读取抖音 Cookie 文件失败: %s", str(exc))
        if persisted_douyin_cookies and DouyinExtractor._parse_cookie_header(persisted_douyin_cookies):
            douyin_cookies_str = persisted_douyin_cookies
            logger.info("🍪 使用已持久化的抖音 Cookie: %s", douyin_cookies_file)
        elif douyin_cookies_str:
            try:
                douyin_cookies_file.parent.mkdir(parents=True, exist_ok=True)
                if "\n" not in douyin_cookies_str and "douyin.com" in douyin_cookies_str:
                    douyin_cookies_str = re.sub(
                        r"\s+((?:#HttpOnly_)?\.?(?:(?:www\.)?douyin\.com|(?:www\.)?iesdouyin\.com)\s)",
                        r"\n\1",
                        douyin_cookies_str,
                    )
                    douyin_cookies_str = douyin_cookies_str.replace("# ", "\n# ").strip()
                douyin_cookies_file.write_text(douyin_cookies_str, encoding="utf-8")
                logger.info("🍪 抖音 Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入抖音 Cookie 文件失败: %s", str(exc))
        douyin_extractor = getattr(self, "douyin_extractor", None)
        if douyin_extractor is not None:
            if hasattr(douyin_extractor, "set_cookie_storage"):
                douyin_extractor.set_cookie_storage(
                    douyin_cookies_file, self._on_douyin_cookie_updated
                )
            if hasattr(douyin_extractor, "configure_cookie_refresh"):
                douyin_extractor.configure_cookie_refresh(
                    self.douyin_auto_refresh_cookies,
                    self.douyin_cookie_refresh_interval_hours,
                )
            douyin_extractor.set_cookie(douyin_cookies_str)
            self.douyin_cookies = douyin_extractor.get_cookies()
            self.douyin_cookie_enabled = douyin_extractor.has_cookie()
        else:
            # 允许仅测试/重载部分配置的轻量实例不创建完整解析器。
            self.douyin_cookies = {}
            self.douyin_cookie_enabled = False

        # 微博配置
        self.weibo_max_media = max(
            1, int(self._get_config_value("weibo_settings.max_media", 99))
        )
        self.weibo_merge_send = bool(
            self._get_config_value("weibo_settings.merge_send", False)
        )
        self.weibo_download_original = bool(
            self._get_config_value("weibo_settings.download_original", True)
        )
        self.weibo_enable_live_photo = bool(
            self._get_config_value("weibo_settings.enable_live_photo", False)
        )
        _weibo_live_photo_format = str(
            self._get_config_value("weibo_settings.live_photo_format", "视频")
        ).strip()
        self.weibo_live_photo_format = (
            _weibo_live_photo_format
            if _weibo_live_photo_format in ("视频", "GIF")
            else "视频"
        )
        self.weibo_auto_refresh_cookies = bool(
            self._get_config_value("weibo_settings.auto_refresh_cookies", True)
        )
        self.weibo_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(
                    self._get_config_value(
                        "weibo_settings.cookie_refresh_interval_hours", 12
                    )
                ),
            ),
        )
        self.weibo_cookie_notification_target = str(
            self._get_config_value("weibo_settings.cookie_notification_target", "")
        ).strip()
        self.weibo_login_account = str(
            self._get_config_value("weibo_settings.login_account", "")
        ).strip()
        self.weibo_login_password = str(
            self._get_config_value("weibo_settings.login_password", "")
        ).strip()
        weibo_cookies_str = str(
            self._get_config_value("weibo_settings.cookies", "")
        ).strip()
        weibo_cookies_file = get_weibo_cookies_file()
        # Preserve automatic renewal for unchanged inputs, but let a new
        # explicitly configured Cookie replace an older local file.
        try:
            weibo_cookies_str = WeiboExtractor.normalize_cookie_source(weibo_cookies_str)
            configured_cookie = weibo_cookies_str
            weibo_cookies_str = load_cookie_source(configured_cookie, weibo_cookies_file)
            if configured_cookie and configured_cookie == weibo_cookies_str:
                logger.debug("🍪 使用后台配置的微博 Cookie")
            elif weibo_cookies_str:
                logger.debug("🍪 使用已持久化的微博 Cookie")
        except OSError as exc:
            logger.warning("⚠️ 读写微博 Cookie 文件失败（%s），使用当前后台配置", type(exc).__name__)
        if hasattr(self.weibo_extractor, "set_cookie_storage"):
            self.weibo_extractor.set_cookie_storage(
                weibo_cookies_file,
                self._on_weibo_cookie_updated,
            )
        if hasattr(self.weibo_extractor, "set_cookie_expired_callback"):
            self.weibo_extractor.set_cookie_expired_callback(
                self._on_weibo_cookie_expired
            )
        if hasattr(self.weibo_extractor, "configure_cookie_refresh"):
            self.weibo_extractor.configure_cookie_refresh(
                self.weibo_auto_refresh_cookies,
                self.weibo_cookie_refresh_interval_hours,
            )
        self.weibo_extractor.set_cookie(weibo_cookies_str)
        self.weibo_extractor.download_original = self.weibo_download_original
        self.weibo_cookie_enabled = self.weibo_extractor.has_user_cookie()

        # X 配置
        self.twitter_max_media = max(
            1, int(self._get_config_value("twitter_settings.max_media", 99))
        )
        self.twitter_merge_send = bool(
            self._get_config_value("twitter_settings.merge_send", False)
        )
        self.twitter_auto_refresh_cookies = bool(
            self._get_config_value("twitter_settings.auto_refresh_cookies", True)
        )
        self.twitter_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(self._get_config_value("twitter_settings.cookie_refresh_interval_hours", 12)),
            ),
        )
        twitter_cookies_str = str(
            self._get_config_value("twitter_settings.cookies", "")
        ).strip()
        twitter_cookies_file = get_twitter_cookies_file()
        persisted_twitter_cookies = ""
        if twitter_cookies_file.exists():
            try:
                persisted_twitter_cookies = twitter_cookies_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("⚠️ 读取 X Cookie 文件失败: %s", str(exc))
        twitter_extractor = getattr(self, "twitter_extractor", None)
        if persisted_twitter_cookies:
            try:
                has_persisted = bool(
                    (twitter_extractor is None)
                    or twitter_extractor._parse_cookie_header(persisted_twitter_cookies)
                )
            except Exception:
                has_persisted = bool(persisted_twitter_cookies)
            if has_persisted:
                twitter_cookies_str = persisted_twitter_cookies
                logger.info("🍪 使用已持久化的 X Cookie: %s", twitter_cookies_file)
        elif twitter_cookies_str:
            try:
                twitter_cookies_file.parent.mkdir(parents=True, exist_ok=True)
                twitter_cookies_file.write_text(twitter_cookies_str, encoding="utf-8")
                logger.info("🍪 X Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 X Cookie 文件失败: %s", str(exc))
        if twitter_extractor is not None:
            if hasattr(twitter_extractor, "set_cookie_storage"):
                twitter_extractor.set_cookie_storage(
                    twitter_cookies_file, self._on_twitter_cookie_updated
                )
            if hasattr(twitter_extractor, "configure_cookie_refresh"):
                twitter_extractor.configure_cookie_refresh(
                    self.twitter_auto_refresh_cookies,
                    self.twitter_cookie_refresh_interval_hours,
                )
            if hasattr(twitter_extractor, "set_cookie"):
                twitter_extractor.set_cookie(twitter_cookies_str)
        self.twitter_cookie_enabled = bool(
            twitter_extractor is not None
            and getattr(twitter_extractor, "has_cookie", lambda: False)()
        )

        # NGA 配置
        self.nga_merge_send = bool(
            self._get_config_value("nga_settings.merge_send", False)
        )
        self.nga_max_attachment_images = max(
            0, int(self._get_config_value("nga_settings.max_attachment_images", 9))
        )
        self.nga_auto_refresh_cookies = bool(
            self._get_config_value("nga_settings.auto_refresh_cookies", True)
        )
        self.nga_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(self._get_config_value("nga_settings.cookie_refresh_interval_hours", 12)),
            ),
        )
        self.nga_cookies = str(self._get_config_value("nga_settings.cookies", "")).strip()
        nga_cookies_file = get_nga_cookies_file()
        persisted_nga_cookies = ""
        if nga_cookies_file.exists():
            try:
                persisted_nga_cookies = nga_cookies_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("⚠️ 读取 NGA Cookie 文件失败: %s", str(exc))
        if persisted_nga_cookies and parse_nga_cookies(persisted_nga_cookies):
            self.nga_cookies = persisted_nga_cookies
            logger.info("🍪 使用已持久化的 NGA Cookie: %s", nga_cookies_file)
        elif self.nga_cookies:
            try:
                nga_cookies_file.parent.mkdir(parents=True, exist_ok=True)
                if "\n" not in self.nga_cookies and ".ngabbs.com" in self.nga_cookies:
                    self.nga_cookies = re.sub(
                        r"\s+(\.?(?:www\.)?(?:ngabbs\.com|nga\.cn|nga\.178\.com)\s)",
                        r"\n\1",
                        self.nga_cookies,
                    )
                    self.nga_cookies = self.nga_cookies.replace("# ", "\n# ").strip()
                nga_cookies_file.write_text(self.nga_cookies, encoding="utf-8")
                logger.info("🍪 NGA Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 NGA Cookie 文件失败: %s", str(exc))

        # 通用站点(yt-dlp 兜底)配置
        generic_height_options = {
            "原画 (最高画质)": 0,
            "1080P": 1080,
            "720P": 720,
            "480P": 480,
        }
        self.generic_max_height = generic_height_options.get(
            str(self._get_config_value("generic_settings.max_height", "720P"))
            .strip()
            .upper(),
            720,
        )
        try:
            self.generic_max_duration_seconds = max(
                0,
                int(
                    self._get_config_value("generic_settings.max_duration_seconds", 300)
                ),
            )
        except (TypeError, ValueError):
            self.generic_max_duration_seconds = 300
        self.generic_merge_send = bool(
            self._get_config_value("generic_settings.merge_send", False)
        )
        self.generic_cookies_file = None
        generic_cookies_text = str(
            self._get_config_value("generic_settings.cookies", "")
        ).strip()
        if generic_cookies_text:
            try:
                cookies_file = get_generic_cookies_file()
                cookies_file.write_text(generic_cookies_text, encoding="utf-8")
                self.generic_cookies_file = str(cookies_file)
            except Exception as exc:
                logger.warning("⚠️ 写入通用站点 Cookie 文件失败: %s", str(exc))
        _blocked_domains = self._get_config_value(
            "generic_settings.blocked_domains", []
        )
        self.generic_extractor.set_blocked_domains(
            _blocked_domains if isinstance(_blocked_domains, list) else []
        )

        # YouTube 配置
        _youtube_height = (
            str(self._get_config_value("youtube_settings.max_height", "1080P"))
            .strip()
            .upper()
        )
        youtube_height_options = {
            "原画 (最高画质)": 0,
            "8K": 4320,
            "4K": 2160,
            "1080P": 1080,
            "720P": 720,
            "480P": 480,
            "360P": 360,
            "240P": 240,
            "144P": 144,
        }
        # 兼容早期配置中的数字分辨率值.
        legacy_youtube_height_options = {
            "0": 0,
            "144": 144,
            "240": 240,
            "360": 360,
            "480": 480,
            "720": 720,
            "1080": 1080,
            "2160": 2160,
            "4320": 4320,
        }
        self.youtube_max_height = youtube_height_options.get(
            _youtube_height,
            legacy_youtube_height_options.get(_youtube_height, 720),
        )
        self.youtube_max_duration_seconds = max(
            0, int(self._get_config_value("youtube_settings.max_duration_seconds", 300))
        )
        _youtube_codec = (
            str(self._get_config_value("youtube_settings.video_codec", "H.264"))
            .strip()
            .upper()
        )
        self.youtube_video_codec = "av1" if _youtube_codec == "AV1" else "h264"
        self.youtube_merge_send = bool(
            self._get_config_value("youtube_settings.merge_send", False)
        )
        self.youtube_cookies_file = None
        youtube_cookies_text = str(
            self._get_config_value("youtube_settings.cookies", "")
        ).strip()
        if youtube_cookies_text:
            try:
                cookies_file = get_youtube_cookies_file()
                cookies_file.write_text(youtube_cookies_text, encoding="utf-8")
                self.youtube_cookies_file = str(cookies_file)
                logger.info("🍪 YouTube Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 YouTube Cookie 文件失败: %s", str(exc))
        _youtube_client = str(
            self._get_config_value("youtube_settings.player_client", "default")
        ).strip()
        self.youtube_player_client = (
            _youtube_client
            if _youtube_client in {"default", "web_embedded"}
            else "default"
        )

        # 小红书配置
        self.xhs_max_media = max(
            1, int(self._get_config_value("xhs_settings.max_media", 99))
        )
        self.xhs_merge_send = bool(
            self._get_config_value("xhs_settings.merge_send", False)
        )
        _xhs_quality = str(
            self._get_config_value("xhs_settings.max_video_quality", "720P")
        ).strip().upper()
        self.xhs_max_video_quality = (
            _xhs_quality if _xhs_quality in XHS_VIDEO_QUALITY_OPTIONS else "720P"
        )
        self.xhs_summary_mode = self._read_summary_mode("xhs_settings.summary_mode")
        self.xhs_render_card = self.xhs_summary_mode == SUMMARY_MODE_CARD
        self.xhs_download_original = bool(
            self._get_config_value("xhs_settings.download_original", True)
        )
        self.xhs_prefer_ci_png = bool(
            self._get_config_value("xhs_settings.prefer_ci_png", True)
        )
        self.xhs_auto_unmerge_threshold_mb = int(
            self._get_config_value("xhs_settings.auto_unmerge_threshold_mb", 50)
        )
        self.xhs_qq_image_size_limit_mb = max(
            0, int(self._get_config_value("xhs_settings.qq_image_size_limit_mb", 30))
        )
        self.xhs_concurrent_download = bool(
            self._get_config_value("xhs_settings.concurrent_download", True)
        )
        self.xhs_enable_live_photo = bool(
            self._get_config_value("xhs_settings.enable_live_photo", False)
        )
        self.xhs_live_photo_video_limit = max(
            0,
            int(self._get_config_value("xhs_settings.live_photo_video_limit", 1)),
        )
        _xhs_live_photo_format = str(
            self._get_config_value("xhs_settings.live_photo_format", "视频")
        ).strip()
        self.xhs_live_photo_format = (
            _xhs_live_photo_format
            if _xhs_live_photo_format in ("视频", "GIF")
            else "视频"
        )
        self.xhs_enable_comment_screenshot = bool(
            self._get_config_value("xhs_settings.enable_comment_screenshot", False)
        )
        self.xhs_comment_screenshot_max = max(
            0, int(self._get_config_value("xhs_settings.comment_screenshot_max", 20))
        )
        _xhs_comment_mode = str(
            self._get_config_value(
                "xhs_settings.comment_screenshot_mode", COMMENT_MODE_WEB
            )
        ).strip()
        self.xhs_comment_screenshot_mode = (
            _xhs_comment_mode
            if _xhs_comment_mode in XHS_COMMENT_MODES
            else COMMENT_MODE_WEB
        )
        self.xhs_auto_refresh_cookies = bool(
            self._get_config_value("xhs_settings.auto_refresh_cookies", True)
        )
        self.xhs_cookie_refresh_interval_hours = max(
            1,
            min(
                168,
                int(self._get_config_value("xhs_settings.cookie_refresh_interval_hours", 12)),
            ),
        )
        self.xhs_cookies = str(self._get_config_value("xhs_settings.cookies", "")).strip()
        xhs_cookies_file = get_xhs_cookies_file()
        persisted_xhs_cookies = ""
        if xhs_cookies_file.exists():
            try:
                persisted_xhs_cookies = xhs_cookies_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("⚠️ 读取小红书 Cookie 文件失败: %s", str(exc))
        if persisted_xhs_cookies and XiaohongshuExtractor._parse_cookie_header(persisted_xhs_cookies):
            self.xhs_cookies = persisted_xhs_cookies
            logger.info("🍪 使用已持久化的小红书 Cookie: %s", xhs_cookies_file)
        elif self.xhs_cookies:
            try:
                xhs_cookies_file.parent.mkdir(parents=True, exist_ok=True)
                if "\n" not in self.xhs_cookies and ".xiaohongshu.com" in self.xhs_cookies:
                    self.xhs_cookies = re.sub(
                        r"\s+(\.?(?:www\.)?xiaohongshu\.com\s)",
                        r"\n\1",
                        self.xhs_cookies,
                    )
                    self.xhs_cookies = self.xhs_cookies.replace("# ", "\n# ").strip()
                xhs_cookies_file.write_text(self.xhs_cookies, encoding="utf-8")
                logger.info("🍪 小红书 Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入小红书 Cookie 文件失败: %s", str(exc))
        xhs_extractor = getattr(self, "xhs_extractor", None)
        if xhs_extractor is not None:
            if hasattr(xhs_extractor, "set_cookie_storage"):
                xhs_extractor.set_cookie_storage(
                    xhs_cookies_file, self._on_xhs_cookie_updated
                )
            if hasattr(xhs_extractor, "configure_cookie_refresh"):
                xhs_extractor.configure_cookie_refresh(
                    self.xhs_auto_refresh_cookies,
                    self.xhs_cookie_refresh_interval_hours,
                )
            if hasattr(xhs_extractor, "set_max_video_quality"):
                xhs_extractor.set_max_video_quality(self.xhs_max_video_quality)
            if hasattr(xhs_extractor, "set_cookie"):
                xhs_extractor.set_cookie(self.xhs_cookies)

        # 通用配置
        self.retry_count = max(
            0, int(self._get_config_value("general_settings.retry_count", 3))
        )
        self.reaction_emoji_enabled = bool(
            self._get_config_value("general_settings.reaction_emoji_enabled", True)
        )
        _raw_list = self._get_config_value(
            "general_settings.reaction_emoji_list", [127827]
        )
        _emoji_list: list[int] = []
        if isinstance(_raw_list, list):
            for item in _raw_list[:5]:
                coerced = self._coerce_positive_int(item, 0)
                if coerced > 0:
                    _emoji_list.append(coerced)
        self.reaction_emoji_list = _emoji_list
        _strategy = str(
            self._get_config_value("general_settings.reaction_emoji_strategy", "随机")
        ).strip()
        self.reaction_emoji_strategy = (
            _strategy if _strategy in ("随机", "顺序循环") else "随机"
        )
        self.reaction_emoji_type = "1"  # 固定值，无需配置
        self.max_video_size_mb = int(
            self._get_config_value("general_settings.max_video_size_mb", 200)
        )
        self.video_relay_umo = str(
            self._get_config_value("general_settings.video_relay_umo", "")
        ).strip()
        self.merge_send_as_sender = bool(
            self._get_config_value("general_settings.merge_send_as_sender", False)
        )
        _mode = str(
            self._get_config_value("general_settings.error_notify_mode", "静默")
        ).strip()
        self.error_notify_mode = _mode if _mode in ("静默", "脱敏", "报错") else "静默"

        # 群过滤配置(黑/白名单)
        _gf_mode = str(self._get_config_value("group_filter.mode", "黑名单")).strip()
        self.group_filter_mode = (
            _gf_mode if _gf_mode in ("黑名单", "白名单") else "黑名单"
        )
        _gf_list = self._get_config_value("group_filter.group_list", [])
        if isinstance(_gf_list, list):
            self.group_filter_list = [
                str(item).strip() for item in _gf_list if str(item).strip()
            ]
        else:
            self.group_filter_list = []

        alias = self._normalize_quality_alias(self.quality_label)
        if alias == "HDR":
            self.allow_hdr = True
        if alias == "DOLBY":
            self.allow_dolby = True

        self.quality_enum_name, self.video_quality = self._resolve_quality(alias)
        self.codecs_enum_name, self.video_codecs = self._resolve_codecs(
            self.codecs_label
        )

        # 构建启用平台列表
        enabled_list = [p for p in PLATFORM_LABELS if p in enable_platforms]
        duration_label = (
            f"{self.bili_max_duration_seconds}s"
            if self.bili_max_duration_seconds > 0
            else "无限制"
        )
        xhs_image_limit_label = (
            f"{self.xhs_qq_image_size_limit_mb}MB"
            if self.xhs_qq_image_size_limit_mb > 0
            else "关闭"
        )
        logger.info(
            "📹 LinkResolver 配置: 平台=%s, B站(画质=%s,合并=%s,摘要=%s,时长<=%s), 抖音(合并=%s,摘要=%s,Cookie=%s), 小红书(上限=%s,原图=%s,摘要=%s,大图转文件=%s,Live图=%s/%s,评论截图=%s/%s/%d条,Cookie=%s), 微博(原图=%s,合并=%s,Live图=%s/%s,Cookie=%s,续期=%s/%dh), X(合并=%s,最多=%d), YouTube(最高=%sp,编码=%s,时长<=%ss,合并=%s), 字体(自动安装=%s,主字体=%s,Emoji=%s), 重试=%d",
            "/".join(enabled_list) if enabled_list else "无",
            self.video_quality.name,
            "开" if self.bili_merge_send else "关",
            "卡片" if self.bili_render_card else "文字",
            duration_label,
            "开" if self.douyin_merge_send else "关",
            "卡片" if self.douyin_render_card else "文字",
            "开" if self.douyin_cookie_enabled else "关",
            self.xhs_max_video_quality,
            "开" if self.xhs_download_original else "关",
            "卡片" if self.xhs_render_card else "文字",
            xhs_image_limit_label,
            "开" if self.xhs_enable_live_photo else "关",
            self.xhs_live_photo_format,
            "开" if self.xhs_enable_comment_screenshot else "关",
            self.xhs_comment_screenshot_mode,
            self.xhs_comment_screenshot_max,
            "开" if self.xhs_cookies else "关",
            "开" if self.weibo_download_original else "关",
            "开" if self.weibo_merge_send else "关",
            "开" if self.weibo_enable_live_photo else "关",
            self.weibo_live_photo_format,
            "开" if self.weibo_cookie_enabled else "关",
            "开" if self.weibo_auto_refresh_cookies else "关",
            self.weibo_cookie_refresh_interval_hours,
            "开" if self.twitter_merge_send else "关",
            self.twitter_max_media,
            self.youtube_max_height or "不限",
            "AV1" if self.youtube_video_codec == "av1" else "H.264",
            self.youtube_max_duration_seconds or "不限",
            "开" if self.youtube_merge_send else "关",
            "开" if self.font_auto_install_enabled else "关",
            "用户配置"
            if self.user_primary_font_ready
            else (
                "插件"
                if self.managed_primary_font_ready
                else ("系统/现有" if self.default_primary_font else "无")
            ),
            "用户配置"
            if self.user_emoji_font_ready
            else (
                "插件"
                if self.managed_emoji_font_ready
                else ("系统" if self.default_emoji_font else "无")
            ),
            self.retry_count,
        )
        logger.info(
            "🚦 群过滤: 模式=%s, 数量=%d",
            self.group_filter_mode,
            len(self.group_filter_list),
        )
        self.xhs_renderer = XiaohongshuCardRenderer(self.default_primary_font)
        self.xhs_comment_screenshotter = XiaohongshuCommentScreenshotter(
            self.default_primary_font
        )
        if hasattr(self.xhs_comment_screenshotter, "set_cookie_storage"):
            self.xhs_comment_screenshotter.set_cookie_storage(
                xhs_cookies_file, self._on_xhs_cookie_updated
            )
        if hasattr(self.xhs_comment_screenshotter, "configure_cookie_refresh"):
            self.xhs_comment_screenshotter.configure_cookie_refresh(
                self.xhs_auto_refresh_cookies,
                self.xhs_cookie_refresh_interval_hours,
            )
        self.nga_screenshotter = NgaScreenshotter()
        if hasattr(self.nga_screenshotter, "set_cookie_storage"):
            self.nga_screenshotter.set_cookie_storage(
                nga_cookies_file, self._on_nga_cookie_updated
            )
        if hasattr(self.nga_screenshotter, "configure_cookie_refresh"):
            self.nga_screenshotter.configure_cookie_refresh(
                self.nga_auto_refresh_cookies,
                self.nga_cookie_refresh_interval_hours,
            )

    def _set_cookie_config(self, section: str, cookie: str) -> None:
        settings = self.config.get(section) if isinstance(self.config, dict) else None
        if isinstance(settings, dict):
            settings["cookies"] = cookie

    def _on_bili_cookie_updated(self, cookie: str) -> None:
        self._set_cookie_config("bili_settings", cookie)

    def _on_douyin_cookie_updated(self, cookie: str) -> None:
        self._set_cookie_config("douyin_settings", cookie)
        extractor = getattr(self, "douyin_extractor", None)
        if extractor is not None and hasattr(extractor, "get_cookies"):
            self.douyin_cookies = extractor.get_cookies()

    def _on_xhs_cookie_updated(self, cookie: str) -> None:
        self._set_cookie_config("xhs_settings", cookie)
        self.xhs_cookies = cookie

    def _on_twitter_cookie_updated(self, cookie: str) -> None:
        self._set_cookie_config("twitter_settings", cookie)

    def _on_nga_cookie_updated(self, cookie: str) -> None:
        self._set_cookie_config("nga_settings", cookie)

    def _on_weibo_cookie_updated(self, cookie: str) -> None:
        """同步服务端续期或交互登录后的 Cookie 到当前可变配置对象。"""
        self._weibo_cookie_expired_notified_fingerprint = None
        self._weibo_cookie_last_notify_at = 0.0
        try:
            record_refreshed_source(cookie, get_weibo_cookies_file())
        except OSError as exc:
            logger.warning("⚠️ 保存微博 Cookie 来源标记失败（%s）", type(exc).__name__)
        settings = (
            self.config.get("weibo_settings")
            if isinstance(self.config, dict)
            else None
        )
        if isinstance(settings, dict):
            settings["cookies"] = cookie

    @staticmethod
    def _parse_weibo_notification_targets(raw: str) -> list[str]:
        targets: list[str] = []
        for token in re.split(r"[\r\n,;]+", str(raw or "")):
            cleaned = token.strip()
            if cleaned and cleaned not in targets:
                targets.append(cleaned)
        return targets

    async def _send_proactive_message(
        self, target_umo: str, components: list[Comp.BaseMessageComponent]
    ) -> bool:
        send_fn = getattr(self.context, "send_message", None)
        if not callable(send_fn):
            logger.warning("⚠️ 当前 AstrBot 上下文不支持主动发送消息，跳过推送: %s", target_umo)
            return False
        try:
            result = await send_fn(target_umo, MessageChain(components))
            return result is not False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 主动发送微博登录/提醒消息失败 (%s): %s", target_umo, exc)
            return False

    async def _on_weibo_cookie_expired(self, reason: str) -> None:
        """当微博 Cookie 过期且无法自动保活时，向管理员会话发送提醒。"""
        targets = self._parse_weibo_notification_targets(
            getattr(self, "weibo_cookie_notification_target", "")
        )
        if not targets:
            return
        cookie_text = str(getattr(self.weibo_extractor, "cookie", "") or "").strip()
        fingerprint = hashlib.sha256(cookie_text.encode("utf-8")).hexdigest()
        now = time.monotonic()
        if (
            self._weibo_cookie_expired_notified_fingerprint == fingerprint
            and now - self._weibo_cookie_last_notify_at < WEIBO_EXPIRE_NOTIFY_COOLDOWN_SEC
        ):
            return

        message = (
            f"⚠️ 微博 Cookie 已失效且无法自动保活：{reason}\n\n"
            "请管理员发送以下指令重新登录（更新后将同步供 weibo_monitor 共用）：\n"
            "• /weibo_login_scan — Playwright 扫码登录（推荐）\n"
            "• /weibo_login_code — 账号密码/验证码登录"
        )
        sent_any = False
        for target in targets:
            if await self._send_proactive_message(target, [Comp.Plain(message)]):
                sent_any = True
        if sent_any:
            self._weibo_cookie_expired_notified_fingerprint = fingerprint
            self._weibo_cookie_last_notify_at = now
            logger.info("📨 已向管理员发送微博 Cookie 过期提醒: targets=%d", len(targets))

    @staticmethod
    def _format_weibo_login_success(summary: dict, mode_label: str) -> str:
        desktop_fields = ", ".join(summary.get("desktop_login_fields") or []) or "无"
        sina_fields = ", ".join(summary.get("sina_cookie_names") or []) or "无"
        tgc_status = "已获取" if summary.get("has_tgc") else "未包含"
        return (
            f"✅ 微博{mode_label}成功，已保存完整 Netscape Cookie（weibo.com + login.sina.com.cn）。\n"
            f"• 总 Cookie 条目：{summary.get('total_cookies', 0)}\n"
            f"• 桌面端登录字段：{desktop_fields}\n"
            f"• 新浪 SSO 字段：{sina_fields}（tgc：{tgc_status}）\n"
            f"• 持久化文件：{get_weibo_cookies_file()}\n"
            "💡 若 astrbot_plugin_weibo_monitor 已开启共用本插件 Cookie，将在下一次请求时自动生效。"
        )

    async def _run_weibo_qr_login_task(self, target_umo: str) -> None:
        try:
            async def on_qr_ready(qr_path: Path) -> None:
                await self._send_proactive_message(
                    target_umo,
                    [
                        Comp.Plain(
                            "📱 请使用微博 App 扫描下方二维码登录（有效时间约 3 分钟，发送 /weibo_login_cancel 可取消）："
                        ),
                        Comp.Image.fromFileSystem(str(qr_path.resolve())),
                    ],
                )

            async def on_status(text: str) -> None:
                await self._send_proactive_message(target_umo, [Comp.Plain(text)])

            result = await self.weibo_login_manager.login_by_qr(
                owner_umo=target_umo,
                qr_dir=get_weibo_cache(),
                on_qr_ready=on_qr_ready,
                on_status=on_status,
            )
            summary = await self.weibo_extractor.apply_interactive_login_result(
                result.cookies, result.storage_state
            )
            self.weibo_cookie_enabled = self.weibo_extractor.has_user_cookie()
            await self._send_proactive_message(
                target_umo,
                [Comp.Plain(self._format_weibo_login_success(summary, "扫码登录"))],
            )
        except asyncio.CancelledError:
            raise
        except (WeiboLoginCancelledError, WeiboLoginTimeoutError, WeiboInteractiveLoginError) as exc:
            await self._send_proactive_message(target_umo, [Comp.Plain(f"⚠️ {exc}")])
        except Exception as exc:
            logger.error("❌ 微博扫码登录异常: %s", exc)
            await self._send_proactive_message(
                target_umo,
                [Comp.Plain(f"❌ 微博扫码登录失败（{type(exc).__name__}）：{exc}")],
            )
        finally:
            if self._weibo_login_task is asyncio.current_task():
                self._weibo_login_task = None

    async def _run_weibo_code_login_task(self, target_umo: str) -> None:
        try:
            async def on_code_requested(prompt: str) -> None:
                await self._send_proactive_message(
                    target_umo, [Comp.Plain(f"🔐 {prompt}")]
                )

            async def on_status(text: str) -> None:
                await self._send_proactive_message(target_umo, [Comp.Plain(text)])

            result = await self.weibo_login_manager.login_by_code(
                account=self.weibo_login_account,
                password=self.weibo_login_password,
                owner_umo=target_umo,
                on_code_requested=on_code_requested,
                on_status=on_status,
            )
            summary = await self.weibo_extractor.apply_interactive_login_result(
                result.cookies, result.storage_state
            )
            self.weibo_cookie_enabled = self.weibo_extractor.has_user_cookie()
            await self._send_proactive_message(
                target_umo,
                [Comp.Plain(self._format_weibo_login_success(summary, "验证码登录"))],
            )
        except asyncio.CancelledError:
            raise
        except (
            WeiboLoginCaptchaBlockedError,
            WeiboLoginCancelledError,
            WeiboLoginTimeoutError,
            WeiboInteractiveLoginError,
        ) as exc:
            await self._send_proactive_message(target_umo, [Comp.Plain(f"⚠️ {exc}")])
        except Exception as exc:
            logger.error("❌ 微博验证码登录异常: %s", exc)
            await self._send_proactive_message(
                target_umo,
                [Comp.Plain(f"❌ 微博验证码登录失败（{type(exc).__name__}）：{exc}")],
            )
        finally:
            if self._weibo_login_task is asyncio.current_task():
                self._weibo_login_task = None

    # endregion

    def _configure_managed_fonts(self) -> None:
        """根据配置决定是否启用用户字体和自动安装插件字体。"""
        custom_primary_font = str(
            self._get_config_value("general_settings.custom_font_path", "")
        ).strip()
        custom_emoji_font = str(
            self._get_config_value("general_settings.custom_emoji_font_path", "")
        ).strip()
        self.custom_primary_font_path = custom_primary_font or None
        self.custom_emoji_font_path = custom_emoji_font or None
        set_user_font_paths(custom_primary_font, custom_emoji_font)

        self.font_auto_install_enabled = bool(
            self._get_config_value("general_settings.auto_install_fonts", False)
        )
        set_managed_fonts_enabled(self.font_auto_install_enabled)
        if not self.font_auto_install_enabled:
            self.managed_primary_font_ready = False
            self.managed_emoji_font_ready = False

    # region 解析任务管理
    def _register_parse_task(
        self, kind: str, event: AstrMessageEvent | None = None
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        message_id = None
        if event is not None:
            message_id = self._extract_reaction_message_id(event)
        tag = f"{kind}:{message_id or 'unknown'}"
        try:
            task.set_name(f"{TASK_NAME_PREFIX}:{tag}:{int(time.time() * 1000)}")
        except Exception:
            pass
        self._active_parse_tasks.add(task)
        task.add_done_callback(lambda t: self._active_parse_tasks.discard(t))

    def _cancel_previous_parse_tasks(self) -> None:
        """通过 asyncio.all_tasks() 按任务名/协程名扫描并取消旧解析任务。

        不依赖 self._active_parse_tasks（调用时该属性尚未初始化）。
        """
        cancelled: list[str] = []
        candidates: set[asyncio.Task] = set()

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

        if loop:
            try:
                current_task = asyncio.current_task(loop=loop)
            except Exception:
                current_task = None
            try:
                tasks = asyncio.all_tasks(loop)
            except Exception:
                tasks = set()
            for task in tasks:
                if task is current_task:
                    continue
                name = task.get_name() if hasattr(task, "get_name") else ""
                if isinstance(name, str) and name.startswith(TASK_NAME_PREFIX):
                    candidates.add(task)
                    continue
                try:
                    qualname = getattr(task.get_coro(), "__qualname__", "")
                except Exception:
                    qualname = ""
                if any(token in qualname for token in TASK_QUALNAME_TOKENS):
                    candidates.add(task)

        for task in candidates:
            if task.done():
                continue
            try:
                task.cancel()
                name = task.get_name() if hasattr(task, "get_name") else ""
                if name:
                    cancelled.append(name)
            except Exception:
                continue

        if cancelled:
            sample = ", ".join(cancelled[:5])
            suffix = "..." if len(cancelled) > 5 else ""
            logger.info(
                "♻️ 插件重载，已中断旧解析任务 %d 个（已进入发送阶段的任务无法终止）: %s%s",
                len(cancelled),
                sample,
                suffix,
            )
        else:
            logger.info("♻️ 插件重载，未发现可中断的旧解析任务")

    # endregion

    # region 通用工具
    def _has_json_component(self, event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(
            event.message_obj, "message"
        ):
            return False
        for component in event.message_obj.message:
            if isinstance(component, dict):
                comp_type = component.get("type")
                if comp_type == "reply":
                    continue
                if comp_type and "json" in str(comp_type).lower():
                    return True
                continue
            if isinstance(component, Comp.Json):
                return True
            comp_type = getattr(component, "type", None)
            if comp_type and "json" in str(comp_type).lower():
                return True
        return False

    @staticmethod
    def _coerce_positive_int(value: object, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        try:
            if isinstance(value, (int, float)):
                parsed = int(value)
                return parsed if parsed > 0 else default
            text = str(value).strip()
            if text.isdigit():
                parsed = int(text)
                return parsed if parsed > 0 else default
        except Exception:
            return default
        return default

    @staticmethod
    def _format_duration(duration_seconds: int | None) -> str | None:
        if not duration_seconds:
            return None
        minutes = int(duration_seconds) // 60
        seconds = int(duration_seconds) % 60
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def _hash_url(url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]

    @staticmethod
    def _guess_media_suffix(url: str, default: str) -> str:
        try:
            suffix = Path(urlparse(url).path).suffix
        except Exception:
            suffix = ""
        if suffix and len(suffix) <= 5:
            return suffix
        return default

    # endregion

    # region 链接提取
    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        if not text:
            return []
        return re.findall(r"https?://[^\s'\"<>]+", text)

    def _coerce_json_payload(self, json_component) -> dict | None:
        def unwrap(value, depth: int = 0) -> dict | None:
            if depth > 4 or value is None:
                return None
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
                try:
                    return unwrap(json.loads(value), depth + 1)
                except Exception:
                    return None
            if isinstance(value, dict):
                if any(
                    key in value
                    for key in ("meta", "prompt", "ver", "app", "view", "config")
                ):
                    return value
                if "data" in value:
                    return unwrap(value["data"], depth + 1)
                return value
            if isinstance(value, list):
                for item in value:
                    payload = unwrap(item, depth + 1)
                    if payload:
                        return payload
            return None

        if hasattr(json_component, "data"):
            return unwrap(json_component.data)
        return unwrap(json_component)

    def extract_links_from_json(self, json_component) -> list[str]:
        links: list[str] = []
        try:
            json_data = self._coerce_json_payload(json_component)
            if not json_data:
                return links

            def search_json_for_links(obj):
                found: list[str] = []
                if isinstance(obj, dict):
                    for value in obj.values():
                        if isinstance(value, str):
                            found.extend(self._extract_urls_from_text(value))
                        elif isinstance(value, (dict, list)):
                            found.extend(search_json_for_links(value))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str):
                            found.extend(self._extract_urls_from_text(item))
                        elif isinstance(item, (dict, list)):
                            found.extend(search_json_for_links(item))
                return found

            links.extend(search_json_for_links(json_data))

            if isinstance(json_data, dict):
                meta = json_data.get("meta", {})
                detail = meta.get("detail_1", {}) if meta else {}
                if detail:
                    for key in ("qqdocurl", "url"):
                        value = detail.get(key, "")
                        if value:
                            links.extend(self._extract_urls_from_text(value))
        except Exception as exc:
            logger.warning("⚠️ 解析 JSON 消息组件失败: %s", str(exc))
        return links

    @staticmethod
    def _is_ignored_card_link(url: str) -> bool:
        try:
            hostname = (urlparse(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in IGNORED_CARD_LINK_DOMAINS
        )

    # endregion

    # region 消息基础判断
    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) == str(event.get_self_id())
        except Exception:
            return False

    async def _is_bot_muted(self, event: AstrMessageEvent) -> bool:
        """检测 Bot 是否在群中被禁言。

        通过 OneBot V11 的 get_group_member_info API 获取 Bot 在群中的信息，
        检查 shut_up_timestamp 字段判断是否被禁言。

        Returns:
            True 如果 Bot 被禁言，False 如果未被禁言或无法检测。
        """
        group_id = event.get_group_id()
        if not group_id:
            return False

        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False

        self_id = event.get_self_id()
        if not self_id:
            return False

        try:
            member_info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(self_id),
                no_cache=True,
            )
            shut_up_timestamp = member_info.get("shut_up_timestamp", 0)
            if shut_up_timestamp and shut_up_timestamp > time.time():
                logger.info("🔇 Bot 在群 %s 中被禁言，跳过处理", group_id)
                return True
        except Exception as exc:
            logger.debug("检测禁言状态失败: %s", str(exc))

        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """根据群过滤(黑/白名单)判断当前群是否允许解析。

        私聊不受过滤限制,始终返回 True。
        """
        group_id = event.get_group_id()
        if not group_id:
            return True
        gid = str(group_id)
        if self.group_filter_mode == "白名单":
            allowed = gid in self.group_filter_list
        else:
            allowed = gid not in self.group_filter_list
        if not allowed:
            logger.debug("🚫 群 %s 被%s拦截,跳过解析", gid, self.group_filter_mode)
        return allowed

    # endregion

    # region 表情回应
    def _extract_reaction_message_id(self, event: AstrMessageEvent) -> int | None:
        raw = getattr(event.message_obj, "raw_message", None)
        candidates: list[object] = []
        if isinstance(raw, dict):
            candidates.append(raw.get("message_id"))
        elif raw is not None and hasattr(raw, "message_id"):
            candidates.append(getattr(raw, "message_id", None))
        candidates.append(getattr(event.message_obj, "message_id", None))
        for value in candidates:
            if value is None:
                continue
            try:
                mid = int(value)
            except Exception:
                continue
            if mid > 0:
                return mid
        return None

    async def _send_reaction_emoji(
        self, event: AstrMessageEvent, source_tag: str
    ) -> None:
        if not self.reaction_emoji_enabled:
            return
        if not self.reaction_emoji_list:
            logger.debug("表情回应跳过%s: 列表为空", source_tag)
            return
        if not event.get_group_id():
            logger.debug("表情回应跳过%s: 非群消息", source_tag)
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "set_msg_emoji_like"):
            logger.debug("表情回应跳过%s: 平台不支持", source_tag)
            return
        message_id = self._extract_reaction_message_id(event)
        if message_id is None:
            logger.debug("表情回应跳过%s: 无法获取 message_id", source_tag)
            return
        if self.reaction_emoji_strategy == "顺序循环":
            emoji_ids = list(self.reaction_emoji_list)
        else:
            emoji_ids = [choice(self.reaction_emoji_list)]
        for emoji_id in emoji_ids:
            try:
                await bot.set_msg_emoji_like(
                    message_id=message_id,
                    emoji_id=emoji_id,
                    emoji_type=self.reaction_emoji_type,
                    set=True,
                )
            except Exception as exc:
                logger.warning(
                    "⚠️ 表情回应失败%s: emoji_id=%s, %s", source_tag, emoji_id, str(exc)
                )
            if len(emoji_ids) > 1:
                await asyncio.sleep(0.5)

    # endregion

    # region 下载工具
    async def _probe_stream_size(
        self,
        url: str,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> int | None:
        try:
            headers = headers or {}
            cookies = cookies or {}
            async with httpx.AsyncClient(
                timeout=10.0, headers=headers, cookies=cookies
            ) as client:
                response = await client.head(url, follow_redirects=True)
                if response.status_code >= 400:
                    return None
                length = response.headers.get("Content-Length")
                if length:
                    return int(length)
                range_headers = {**headers, "Range": "bytes=0-0"}
                response = await client.get(url, headers=range_headers)
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    return int(content_range.split("/")[-1])
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return None

    async def _estimate_total_size_mb(
        self,
        video_url: str,
        audio_url: str | None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> float | None:
        total = 0
        unknown = False
        for url in (video_url, audio_url):
            if not url:
                continue
            size = await self._probe_stream_size(url, cookies=cookies, headers=headers)
            if size is None:
                unknown = True
                continue
            total += size
        if total == 0 and unknown:
            return None
        return total / 1024 / 1024

    async def _download_stream(
        self,
        url: str,
        output_path: Path,
        cookies: dict[str, str] | None,
        max_bytes: int | None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> int:
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                hdrs = headers or {}
                cks = cookies or {}
                async with httpx.AsyncClient(
                    timeout=None, headers=hdrs, cookies=cks
                ) as client:
                    async with client.stream(
                        "GET", url, follow_redirects=True
                    ) as response:
                        response.raise_for_status()
                        content_length = response.headers.get("Content-Length")
                        if (
                            content_length
                            and max_bytes
                            and int(content_length) > max_bytes
                        ):
                            raise SizeLimitExceeded("超过大小限制")
                        bytes_written = 0

                        # Actually, wrapping each write is fine if chunks are large
                        with open(temp_path, "wb") as file:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if not chunk:
                                    continue
                                bytes_written += len(chunk)
                                if max_bytes and bytes_written > max_bytes:
                                    raise SizeLimitExceeded("超过大小限制")
                                await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(temp_path.replace, output_path)
                return bytes_written
            except asyncio.CancelledError:
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                raise
            except SizeLimitExceeded:
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                raise
            except Exception as exc:
                last_error = exc
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                # 4xx(除 408/429)多为地址过期/签名或 Referer 不被接受, 重试同一地址无意义,
                # 直接抛出, 让调用方切换候选地址
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and 400 <= exc.response.status_code < 500
                    and exc.response.status_code not in (408, 429)
                ):
                    logger.debug(
                        "⏭️ 下载返回 %s, 不重试同一地址: %s",
                        exc.response.status_code,
                        url[:120],
                    )
                    raise
                if attempt < retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "⚠️ 下载失败, %d秒后重试 (%d/%d): %s",
                        wait_time,
                        attempt + 1,
                        retries,
                        str(exc),
                    )
                    await asyncio.sleep(wait_time)
        if last_error:
            raise last_error
        raise RuntimeError("下载失败")

    async def _merge_av(self, v_path: Path, a_path: Path, output_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(v_path),
            "-i",
            str(a_path),
            "-c",
            "copy",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            str(output_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(stderr.decode().strip())
        finally:
            await asyncio.to_thread(v_path.unlink, missing_ok=True)
            await asyncio.to_thread(a_path.unlink, missing_ok=True)

    async def download_thumbnail(self, url: str, save_path: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    await asyncio.to_thread(save_path.write_bytes, response.content)
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 下载封面失败: %s", str(exc))
        return False

    async def calculate_md5(self, file_path: Path) -> str:
        def _sync_md5():
            hasher = hashlib.md5()
            with open(file_path, "rb") as file:
                while chunk := file.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()

        return await asyncio.to_thread(_sync_md5)

    async def cleanup_files(
        self, video_paths: list[Path], thumbnail_paths: list[Path]
    ) -> None:
        # Direct Send Pattern: 调用此方法时，文件已通过 await event.send() 被读取完毕
        # 无需延迟，立即清理以避免与后续相同 URL 请求产生竞态条件
        for video_path in video_paths:
            existed = await asyncio.to_thread(video_path.exists)
            await asyncio.to_thread(video_path.unlink, missing_ok=True)
            logger.debug("🧹 清理视频文件: path=%s, existed=%s", video_path, existed)
        for thumb_path in thumbnail_paths:
            existed = await asyncio.to_thread(thumb_path.exists)
            await asyncio.to_thread(thumb_path.unlink, missing_ok=True)
            logger.debug("🧹 清理缩略图文件: path=%s, existed=%s", thumb_path, existed)

    # endregion

    # region 合并转发发送人获取
    def _get_merge_sender_uin(self, event: AstrMessageEvent) -> str:
        """获取合并转发使用的 uin

        根据 merge_send_as_sender 配置决定使用发送者的 uin 还是 Bot 的 uin
        """
        if self.merge_send_as_sender:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
        return str(event.get_self_id())

    async def _prepare_component_for_merge_send(
        self, component: Comp.BaseMessageComponent
    ) -> Comp.BaseMessageComponent:
        """将合并转发中的本地媒体转换为文件服务地址。

        合并转发节点可能只调用同步的 ``toDict``，异机适配器无法读取
        ``file:///`` 本地路径。图片和视频都需要在发送前注册到 AstrBot
        文件服务；注册失败时保留原组件，兼容同机直读本地文件的适配器。
        """

        if not isinstance(component, (Comp.Image, Comp.Video)):
            return component

        file_ref = str(getattr(component, "file", "") or "").strip()
        if not file_ref or file_ref.startswith(("http://", "https://", "base64://")):
            return component

        try:
            callback_url = await component.register_to_file_service()
        except Exception as exc:
            media_label = "图片" if isinstance(component, Comp.Image) else "视频"
            logger.debug(
                "⏭️ 合并转发%s保留本地路径: %s", media_label, str(exc)
            )
            return component

        logger.debug(
            "🔗 合并转发%s改用回调地址: %s",
            "图片" if isinstance(component, Comp.Image) else "视频",
            callback_url,
        )
        if isinstance(component, Comp.Image):
            return Comp.Image.fromURL(callback_url)
        return Comp.Video.fromURL(
            callback_url,
            cover=getattr(component, "cover", ""),
            c=getattr(component, "c", 2),
        )

    async def _relay_video_reference_nodes(
        self,
        event: AstrMessageEvent,
        videos: list[Comp.Video],
        source_tag: str = "",
    ) -> list[RelayVideoReferenceNode] | None:
        """Upload videos to the configured QQ private session and return forward refs.

        The adapter's send action returns the message ID for each upload. Keeping
        that ID with the current parse request avoids matching against shared
        relay-chat history, which could race when multiple links are processed.
        """

        if not self.video_relay_umo or not videos:
            return None

        try:
            references = await send_videos_to_private_relay(
                event, self.video_relay_umo, videos
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "⚠️ 视频中转失败%s，回退为单独发送: %s", source_tag, str(exc)
            )
            return None

        logger.debug(
            "📨 视频已上传到 QQ 私聊中转%s: 数量=%d, UMO=%s",
            source_tag,
            len(references),
            self.video_relay_umo,
        )
        return references

    async def _relay_component_reference_nodes(
        self,
        event: AstrMessageEvent,
        components: list[Comp.BaseMessageComponent],
        source_tag: str = "",
    ) -> list[RelayVideoReferenceNode] | None:
        """Upload components in order to the configured QQ private session and return forward refs."""

        if not self.video_relay_umo or not components:
            return None

        try:
            references = await send_components_to_private_relay(
                event, self.video_relay_umo, components
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "⚠️ 内容中转失败%s，回退为逐条发送: %s", source_tag, str(exc)
            )
            return None

        logger.debug(
            "📨 内容已上传到 QQ 私聊中转%s: 数量=%d, UMO=%s",
            source_tag,
            len(references),
            self.video_relay_umo,
        )
        return references

    # endregion

    # region 微博交互登录命令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_login_scan")
    async def cmd_weibo_login_scan(self, event: AstrMessageEvent):
        """管理员触发 Playwright 微博扫码登录。"""
        self._refresh_config()
        if self.weibo_login_manager.is_active() or (
            self._weibo_login_task is not None and not self._weibo_login_task.done()
        ):
            yield event.plain_result(
                "⚠️ 当前已有进行中的微博登录任务，请先完成或发送 /weibo_login_cancel 取消。"
            )
            return
        if await self.weibo_extractor.is_user_cookie_valid():
            yield event.plain_result("✅ 当前微博 Cookie 仍有效，无需重新登录。")
            return

        target_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not target_umo:
            yield event.plain_result("❌ 无法获取当前会话 UMO，无法启动扫码登录。")
            return

        self._weibo_login_task = asyncio.create_task(
            self._run_weibo_qr_login_task(target_umo),
            name="link-resolver-weibo-qr-login",
        )
        yield event.plain_result("🔄 正在启动 Playwright 获取微博登录二维码，请稍候...")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_login_code")
    async def cmd_weibo_login_code(self, event: AstrMessageEvent):
        """管理员触发 Playwright 微博账号密码/验证码登录。"""
        self._refresh_config()
        if self.weibo_login_manager.is_active() or (
            self._weibo_login_task is not None and not self._weibo_login_task.done()
        ):
            yield event.plain_result(
                "⚠️ 当前已有进行中的微博登录任务，请先完成或发送 /weibo_login_cancel 取消。"
            )
            return
        if await self.weibo_extractor.is_user_cookie_valid():
            yield event.plain_result("✅ 当前微博 Cookie 仍有效，无需重新登录。")
            return
        if not self.weibo_login_account:
            yield event.plain_result(
                "⚠️ 尚未配置微博登录账号，请先在插件后台 weibo_settings.login_account"
                "（及可选 login_password）中配置后再执行 /weibo_login_code，或改用 /weibo_login_scan 扫码登录。"
            )
            return

        target_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not target_umo:
            yield event.plain_result("❌ 无法获取当前会话 UMO，无法启动验证码登录。")
            return

        self._weibo_login_task = asyncio.create_task(
            self._run_weibo_code_login_task(target_umo),
            name="link-resolver-weibo-code-login",
        )
        yield event.plain_result("🔄 正在启动 Playwright 执行微博账号/验证码登录，请稍候...")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_code")
    async def cmd_weibo_code(self, event: AstrMessageEvent, code: str = ""):
        """向等待中的微博验证码登录会话提交验证码。"""
        raw = str(code or "").strip()
        if not raw:
            msg = str(getattr(event, "message_str", "") or "").strip()
            parts = msg.split(None, 1)
            raw = parts[1].strip() if len(parts) > 1 else ""
        if not raw:
            yield event.plain_result("⚠️ 请提供验证码，例如：/weibo_code 123456")
            return
        if not self.weibo_login_manager.is_awaiting_code():
            yield event.plain_result("⚠️ 当前没有等待输入验证码的微博登录任务。")
            return
        if self.weibo_login_manager.submit_code(raw):
            yield event.plain_result("✅ 已收到验证码，正在提交微博登录...")
        else:
            yield event.plain_result("⚠️ 提交验证码失败，请确认登录会话是否仍在等待。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_login_cancel")
    async def cmd_weibo_login_cancel(self, event: AstrMessageEvent):
        """取消当前正在进行的微博交互登录任务。"""
        cancelled = self.weibo_login_manager.cancel()
        if self._weibo_login_task is not None and not self._weibo_login_task.done():
            self._weibo_login_task.cancel()
            cancelled = True
        if cancelled:
            yield event.plain_result("🛑 已取消当前微博登录任务。")
        else:
            yield event.plain_result("ℹ️ 当前没有正在进行的微博登录任务。")

    @filter.regex(r"^\s*\d{4,8}\s*$", priority=20)
    async def handle_weibo_verification_code(self, event: AstrMessageEvent):
        """在等待管理员回复验证码期间，直接捕获同一会话发送的纯数字验证码。"""
        if self._is_self_message(event):
            return
        target_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not target_umo or not self.weibo_login_manager.is_awaiting_code(target_umo):
            return
        code = str(getattr(event, "message_str", "") or "").strip()
        if self.weibo_login_manager.submit_code(code, umo=target_umo):
            if hasattr(event, "stop_event"):
                event.stop_event()
            yield event.plain_result("✅ 已收到验证码，正在提交微博登录...")

    # endregion

    # region 事件处理器
    # 每个平台的 @filter.regex 处理器必须静态声明(框架限制), 但都只做三件事:
    # 过滤 -> 登记任务 -> 交给 _guarded / _guarded_stream 执行(统一兜底).
    @filter.regex(BILI_MESSAGE_PATTERN, priority=10)
    async def handle_bili_video(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("bili", event)
        await self._guarded("bili", event, BilibiliMixin.handle_bili_video(self, event))

    @filter.regex(DOUYIN_MESSAGE_PATTERN, priority=10)
    async def handle_douyin(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("douyin", event)
        await self._guarded("douyin", event, DouyinMixin.handle_douyin(self, event))

    @filter.regex(XHS_MESSAGE_PATTERN, priority=10)
    async def handle_xhs(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("xhs", event)
        async for result in self._guarded_stream(
            "xhs", event, XiaohongshuMixin.handle_xhs(self, event)
        ):
            yield result

    @filter.regex(WEIBO_MESSAGE_PATTERN, priority=10)
    async def handle_weibo(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("weibo", event)
        await self._guarded("weibo", event, WeiboMixin.handle_weibo(self, event))

    @filter.regex(TWITTER_MESSAGE_PATTERN, priority=10)
    async def handle_twitter(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("twitter", event)
        await self._guarded("twitter", event, TwitterMixin.handle_twitter(self, event))

    @filter.regex(YOUTUBE_MESSAGE_PATTERN, priority=10)
    async def handle_youtube(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        self._register_parse_task("youtube", event)
        await self._guarded("youtube", event, YoutubeMixin.handle_youtube(self, event))

    @filter.regex(GENERIC_URL_PATTERN, priority=10)
    async def handle_generic(self, event: AstrMessageEvent):
        if self._has_json_component(event) or not self._is_group_allowed(event):
            return
        # 正则对任意链接都会触发; 未启用或不在白名单时 GenericMixin 内部会立即返回,
        # 所以这里放在域名预检之后再登记任务, 避免给无关消息登记解析任务.
        if not self.generic_enabled or not self.generic_extractor.extract_links(
            event.message_str
        ):
            return
        self._register_parse_task("generic", event)
        await self._guarded("generic", event, GenericMixin.handle_generic(self, event))

    # endregion

    # region 统一兜底
    def _report_unhandled_error(
        self, key: str, event: AstrMessageEvent, exc: Exception
    ) -> None:
        """记录未被平台流程处理的异常, 并按 error_notify_mode 决定是否通知群聊.

        典型来源: napcat 发送大文件超时(ActionFailed retcode=1200)、
        解析库抛出的非预期异常. 这些异常若冒到框架会表现为 Star handle error.
        """
        label = PLATFORM_BY_KEY[key].label if key in PLATFORM_BY_KEY else key
        logger.error(
            "❌ %s 处理出现未捕获异常(已兜底): %s: %s",
            label,
            type(exc).__name__,
            str(exc)[:300],
        )
        logger.debug("🔍 %s 异常堆栈:\n%s", label, traceback.format_exc())
        try:
            if self.error_notify_mode == "报错":
                event.set_result(event.plain_result(f"❌ {label} 处理失败: {exc}"))
            elif self.error_notify_mode == "脱敏":
                event.set_result(event.plain_result(f"❌ {label} 处理失败"))
        except Exception as notify_exc:
            logger.warning("⚠️ 发送失败通知时出错: %s", str(notify_exc))

    async def _guarded(self, key: str, event: AstrMessageEvent, awaitable) -> None:
        """执行平台协程, 吞掉非取消类异常(取消异常照常向上传播)."""
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_unhandled_error(key, event, exc)

    async def _guarded_stream(
        self, key: str, event: AstrMessageEvent, agen, error_result=None
    ):
        """_guarded 的异步生成器版本(用于 yield 结果的平台, 如小红书).

        error_result 非 None 时, 兜底异常后会额外产出它(卡片分发用它标记"已处理").
        """
        try:
            async for item in agen:
                yield item
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_unhandled_error(key, event, exc)
            if error_result is not None:
                yield error_result

    # endregion

    # region JSON 卡片分发
    # 每个 _card_* 都是异步生成器: 产出的结果转发给框架, 处理完毕后产出 CARD_HANDLED.
    # 没产出 CARD_HANDLED 表示"链接找到但无法解析", 分发循环会继续尝试下一个平台.
    async def _card_bili(self, event: AstrMessageEvent, links: list[str]):
        ref = await self._resolve_video_ref_from_links(links)
        if not ref:
            logger.warning("⚠️ 从卡片中找到 B 站链接但无法解析: %s", links)
            return
        await self._process_bili_video(event, ref=ref, is_from_card=True)
        yield CARD_HANDLED

    async def _card_stream(self, process, event: AstrMessageEvent, links: list[str]):
        async for result in process(event, links[0], is_from_card=True):
            yield result
        yield CARD_HANDLED

    async def _card_call(self, process, event: AstrMessageEvent, links: list[str]):
        await process(event, links[0], is_from_card=True)
        yield CARD_HANDLED

    def _card_runner(self, key: str):
        """按平台返回卡片处理器; 新平台默认走 _card_call(调用 _process_<key>)."""
        platform = PLATFORM_BY_KEY[key]
        if key == "bili":
            return self._card_bili
        process = getattr(self, platform.process_name)
        if key == "xhs":
            return lambda event, links: self._card_stream(process, event, links)
        return lambda event, links: self._card_call(process, event, links)

    @filter.regex(NGA_MESSAGE_PATTERN, priority=10)
    async def handle_nga(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        if not self._is_group_allowed(event):
            return
        self._register_parse_task("nga", event)
        await self._guarded("nga", event, NgaMixin.handle_nga(self, event))

    @filter.regex(r".*")
    async def handle_json_card(self, event: AstrMessageEvent):
        if self._is_self_message(event):
            return
        if not self._is_group_allowed(event):
            return

        links: list[str] = []
        has_json_component = False
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for component in event.message_obj.message:
                is_json_component = False
                comp_payload = component
                if isinstance(component, dict):
                    comp_type = component.get("type")
                    if (
                        comp_type == "reply"
                    ):  # 忽略引用回复组件，防止回复时递归解析原消息
                        continue
                    comp_payload = component.get("data") or component
                    is_json_component = (
                        bool(comp_type) and "json" in str(comp_type).lower()
                    )
                else:
                    if isinstance(component, Comp.Json):
                        is_json_component = True
                    comp_type = getattr(component, "type", None)
                    if not is_json_component and comp_type:
                        is_json_component = "json" in str(comp_type).lower()
                    if is_json_component and hasattr(component, "data"):
                        comp_payload = component.data
                if is_json_component:
                    has_json_component = True
                    logger.info("🔗 检测到 JSON 卡片消息: %s", component)
                    links.extend(self.extract_links_from_json(comp_payload))
        if not has_json_component:
            return

        if await self._is_bot_muted(event):
            return

        if not links:
            return
        unique_links = list(dict.fromkeys(links))
        candidate_links = [
            link for link in unique_links if not self._is_ignored_card_link(link)
        ]
        if not candidate_links:
            return
        grouped = classify_links(
            candidate_links,
            link_filters={"generic": self.generic_extractor.accepts}
            if self.generic_enabled
            else None,
        )

        for platform in PLATFORMS:
            platform_links = grouped.get(platform.key)
            if not platform_links or not getattr(self, platform.enabled_attr):
                continue
            self._register_parse_task(f"json-{platform.key}", event)
            event.should_call_llm(True)
            handled = False
            try:
                async for result in self._guarded_stream(
                    platform.key,
                    event,
                    self._card_runner(platform.key)(event, platform_links),
                    error_result=CARD_HANDLED,
                ):
                    if result is CARD_HANDLED:
                        handled = True
                        continue
                    yield result
            except asyncio.CancelledError:
                logger.info("♻️ JSON卡片解析任务已中断")
                return
            if handled:
                return

        logger.warning("⚠️ 从卡片中找到链接但无法解析: %s", candidate_links)

    # endregion


LinkResolver = LinkResolverPlugin
Main = LinkResolverPlugin


# endregion
