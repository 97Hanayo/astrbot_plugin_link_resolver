"""Playwright-backed interactive Weibo login (QR scan & account/verification code)."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

from ..common.playwright_manager import (
    browser_channel_candidates,
    configure_playwright_browser_path,
    launch_chromium,
)
from .cookie_storage import filter_weibo_browser_cookies, is_desktop_weibo_domain

WEIBO_SIGNIN_URL = (
    "https://passport.weibo.com/sso/signin"
    "?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F"
)
WEIBO_HOME_URL = "https://weibo.com/"
WEIBO_LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
WEIBO_DESKTOP_LOGIN_MARKERS = ("SUB",)

_QR_TAB_SELECTORS = (
    "text=扫码登录",
    "text=二维码登录",
    "[action-type='switch_qrcode']",
    "[data-tab='qrcode']",
)
_QR_IMAGE_SELECTORS = (
    "img[src*='qrcode']",
    "img[src*='v2/qrcode']",
    "img[src*='qr']",
    ".qrcode img",
    "[class*='qrcode'] img",
    "[class*='qr'] img",
    "[class*='qrcode'] canvas",
    "[class*='qr'] canvas",
)
_QR_REFRESH_SELECTORS = (
    "text=刷新二维码",
    "text=点击刷新",
    "text=刷新",
    "[action-type='refresh_qrcode']",
)

_PASSWORD_TAB_SELECTORS = (
    "text=账号登录",
    "text=密码登录",
    "text=账号密码登录",
    "[action-type='switch_login']",
)
_SMS_TAB_SELECTORS = (
    "text=短信登录",
    "text=验证码登录",
    "text=短信验证码登录",
)
_ACCOUNT_INPUT_SELECTORS = (
    "input[name='username']",
    "input[name='phone']",
    "input[placeholder*='手机号']",
    "input[placeholder*='邮箱']",
    "input[placeholder*='账号']",
    "input[type='tel']",
    "input[type='text']",
)
_PASSWORD_INPUT_SELECTORS = (
    "input[name='password']",
    "input[type='password']",
    "input[placeholder*='密码']",
)
_CODE_INPUT_SELECTORS = (
    "input[name='code']",
    "input[name='verifyCode']",
    "input[name='smsCode']",
    "input[placeholder*='验证码']",
    "input[placeholder*='短信']",
    "input[maxlength='6']",
)
_SEND_CODE_BUTTON_SELECTORS = (
    "button:has-text('获取验证码')",
    "a:has-text('获取验证码')",
    "button:has-text('发送验证码')",
    "a:has-text('发送验证码')",
    "button:has-text('免费获取')",
    "a:has-text('免费获取')",
    "button:has-text('获取短信验证码')",
    "a:has-text('获取短信验证码')",
)
_SUBMIT_BUTTON_SELECTORS = (
    "button[type='submit']",
    "a[action-type='btn_submit']",
    "button:has-text('登录')",
    "a:has-text('登录')",
    "button:has-text('确定')",
    "a:has-text('确定')",
    "button:has-text('验证')",
    "a:has-text('验证')",
    "button:has-text('下一步')",
    "a:has-text('下一步')",
)
_CAPTCHA_SELECTORS = (
    ".geetest_panel",
    ".geetest_widget",
    "[class*='geetest']",
    "[id*='geetest']",
    "iframe[src*='captcha']",
    "iframe[src*='geetest']",
    "[class*='captcha-slider']",
    "[class*='slide-verify']",
    "text=请拖动滑块",
    "text=拖动下方滑块",
    "text=请按顺序点击",
    "text=请完成安全验证",
)
_ERROR_MESSAGE_PATTERNS = (
    "用户名或密码错误",
    "账号或密码错误",
    "密码错误",
    "验证码错误",
    "验证码已过期",
    "验证码无效",
    "手机号格式",
    "发送过于频繁",
    "操作过于频繁",
    "登录失败",
)


class WeiboInteractiveLoginError(RuntimeError):
    """Base error for interactive Weibo login."""


class WeiboLoginCaptchaBlockedError(WeiboInteractiveLoginError):
    """Raised when Weibo presents a slider/image anti-bot challenge."""


class WeiboLoginTimeoutError(WeiboInteractiveLoginError):
    """Raised when the user does not finish login in time."""


class WeiboLoginCancelledError(WeiboInteractiveLoginError):
    """Raised when the login session is cancelled."""


@dataclass(slots=True)
class WeiboLoginResult:
    """Cookies and storage state collected from a completed Weibo login."""

    cookies: list[dict[str, Any]]
    storage_state: dict[str, Any] = field(default_factory=dict)

    @property
    def desktop_cookies(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for cookie in sorted(
            self.cookies, key=lambda c: str(c.get("domain", "")) != ".weibo.com"
        ):
            if (
                is_desktop_weibo_domain(str(cookie.get("domain") or ""))
                and str(cookie.get("path") or "/") == "/"
            ):
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if name and value:
                    result[name] = value
        return result

    @property
    def sina_cookie_names(self) -> list[str]:
        return sorted(
            {
                str(cookie.get("name") or "")
                for cookie in self.cookies
                if str(cookie.get("domain") or "").lstrip(".")
                in {"sina.com.cn", "login.sina.com.cn"}
                and cookie.get("value")
            }
        )


@dataclass
class _ActiveLoginSession:
    mode: str
    owner_umo: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    code_future: asyncio.Future[str] | None = None


def _is_weibo_home_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"weibo.com", "www.weibo.com"}:
        return False
    path = (parsed.path or "/").lower()
    blocked_prefixes = ("/login", "/newlogin", "/signup", "/sso")
    return not any(path.startswith(prefix) for prefix in blocked_prefixes)


def _has_desktop_login_cookies(cookies: list[dict[str, Any]]) -> bool:
    desktop: dict[str, str] = {}
    for cookie in cookies:
        if (
            is_desktop_weibo_domain(str(cookie.get("domain") or ""))
            and str(cookie.get("path") or "/") == "/"
        ):
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name and value:
                desktop[name] = value
    return all(bool(desktop.get(name)) for name in WEIBO_DESKTOP_LOGIN_MARKERS)


async def _find_first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=400):
                return locator
        except Exception:
            continue
    return None


async def _detect_captcha_challenge(page) -> bool:
    for selector in _CAPTCHA_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


async def _detect_page_error_text(page) -> str | None:
    try:
        body_text = await page.inner_text("body", timeout=1000)
    except Exception:
        return None
    if not body_text:
        return None
    for line in body_text.splitlines():
        cleaned = line.strip()
        if not cleaned or len(cleaned) > 120:
            continue
        for pattern in _ERROR_MESSAGE_PATTERNS:
            if pattern in cleaned:
                return cleaned
    return None


async def _is_logged_in(page, context) -> bool:
    url = str(getattr(page, "url", "") or "")
    if not _is_weibo_home_url(url):
        return False
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    return _has_desktop_login_cookies(cookies)


async def _capture_qr_screenshot(page, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Try exact QR element first; wait briefly for QR image to load.
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        qr_el = await _find_first_visible(page, _QR_IMAGE_SELECTORS)
        if qr_el is not None:
            try:
                tag = await qr_el.evaluate("el => el.tagName.toLowerCase()")
                ready = True
                if tag == "img":
                    ready = bool(
                        await qr_el.evaluate(
                            "el => el.complete && el.naturalWidth > 40 && el.naturalHeight > 40"
                        )
                    )
                if ready:
                    await qr_el.screenshot(path=str(output_path))
                    if output_path.exists() and output_path.stat().st_size > 0:
                        return output_path
            except Exception:
                pass
        await asyncio.sleep(0.5)

    # Fallback: capture full viewport so the admin can still scan the visible QR code.
    await page.screenshot(path=str(output_path), full_page=False)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise WeiboInteractiveLoginError("无法截取微博登录二维码图片")
    return output_path


async def _collect_final_login_result(page, context) -> WeiboLoginResult:
    try:
        if not _is_weibo_home_url(str(getattr(page, "url", "") or "")):
            await page.goto(WEIBO_HOME_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    except Exception:
        pass

    raw_cookies = await context.cookies()
    filtered = filter_weibo_browser_cookies(raw_cookies)
    if not _has_desktop_login_cookies(filtered):
        raise WeiboInteractiveLoginError("登录完成但未获取到有效的微博登录 Cookie（缺少 SUB）")
    try:
        storage_state = await context.storage_state()
    except Exception:
        storage_state = {"cookies": filtered, "origins": []}
    return WeiboLoginResult(cookies=filtered, storage_state=storage_state)


class WeiboInteractiveLoginManager:
    """Coordinates interactive Playwright Weibo login sessions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session: _ActiveLoginSession | None = None

    def is_active(self) -> bool:
        return self._session is not None

    @property
    def active_mode(self) -> str | None:
        return self._session.mode if self._session is not None else None

    def is_awaiting_code(self, umo: str | None = None) -> bool:
        session = self._session
        if session is None or session.code_future is None or session.code_future.done():
            return False
        if umo is not None and session.owner_umo and session.owner_umo != umo:
            return False
        return True

    def submit_code(self, code: str, umo: str | None = None) -> bool:
        session = self._session
        if session is None or session.code_future is None or session.code_future.done():
            return False
        if umo is not None and session.owner_umo and session.owner_umo != umo:
            return False
        cleaned = re.sub(r"\s+", "", str(code or ""))
        if not cleaned:
            return False
        session.code_future.set_result(cleaned)
        return True

    def cancel(self) -> bool:
        session = self._session
        if session is None:
            return False
        session.cancel_event.set()
        if session.code_future is not None and not session.code_future.done():
            session.code_future.cancel()
        return True

    async def _wait_for_code(
        self, session: _ActiveLoginSession, timeout_sec: float
    ) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        session.code_future = future
        cancel_task = asyncio.create_task(session.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task},
                timeout=max(1.0, float(timeout_sec)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if session.cancel_event.is_set():
                raise WeiboLoginCancelledError("微博登录已取消")
            if future in done:
                if future.cancelled():
                    raise WeiboLoginCancelledError("微博登录已取消")
                return future.result()
            raise WeiboLoginTimeoutError("等待验证码超时，已关闭登录会话")
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
            session.code_future = None

    async def login_by_qr(
        self,
        *,
        owner_umo: str,
        qr_dir: Path,
        on_qr_ready: Callable[[Path], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]] | None = None,
        timeout_sec: float = 180.0,
        poll_interval_sec: float = 1.5,
        user_agent: str = WEIBO_LOGIN_USER_AGENT,
    ) -> WeiboLoginResult:
        """Open Weibo login page in Playwright, emit QR screenshot, wait for scan."""
        if self._lock.locked():
            raise WeiboInteractiveLoginError("当前已有正在进行的微博登录任务，请先完成或发送 /weibo_login_cancel 取消")

        async with self._lock:
            session = _ActiveLoginSession(mode="scan", owner_umo=owner_umo)
            self._session = session
            temp_qr_files: list[Path] = []
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:
                self._session = None
                raise WeiboInteractiveLoginError(
                    "Playwright 不可用，无法执行微博扫码登录"
                ) from exc

            configure_playwright_browser_path()
            try:
                async with async_playwright() as playwright:
                    browser = await launch_chromium(
                        playwright,
                        headless=True,
                        fallback_executable_paths=browser_channel_candidates(),
                    )
                    try:
                        context = await browser.new_context(
                            viewport={"width": 1280, "height": 900},
                            locale="zh-CN",
                            user_agent=user_agent,
                        )
                        try:
                            page = await context.new_page()
                            try:
                                await page.goto(
                                    WEIBO_SIGNIN_URL,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=6000)
                                except Exception:
                                    pass

                                qr_el = await _find_first_visible(page, _QR_IMAGE_SELECTORS)
                                if qr_el is None:
                                    qr_tab = await _find_first_visible(page, _QR_TAB_SELECTORS)
                                    if qr_tab is not None:
                                        await qr_tab.click()
                                        await asyncio.sleep(1.0)

                                qr_path = qr_dir / f"weibo_qr_{uuid.uuid4().hex[:8]}.png"
                                temp_qr_files.append(qr_path)
                                await _capture_qr_screenshot(page, qr_path)
                                await on_qr_ready(qr_path)

                                deadline = time.monotonic() + max(5.0, float(timeout_sec))
                                qr_refreshes = 0
                                while time.monotonic() < deadline:
                                    if session.cancel_event.is_set():
                                        raise WeiboLoginCancelledError("微博扫码登录已取消")

                                    if await _is_logged_in(page, context):
                                        return await _collect_final_login_result(page, context)

                                    # Refresh QR code once if Weibo marks it expired.
                                    if qr_refreshes < 1:
                                        refresh_btn = await _find_first_visible(
                                            page, _QR_REFRESH_SELECTORS
                                        )
                                        if refresh_btn is not None:
                                            try:
                                                await refresh_btn.click()
                                                await asyncio.sleep(1.5)
                                                qr_refreshes += 1
                                                new_qr = (
                                                    qr_dir
                                                    / f"weibo_qr_{uuid.uuid4().hex[:8]}.png"
                                                )
                                                temp_qr_files.append(new_qr)
                                                await _capture_qr_screenshot(page, new_qr)
                                                if on_status is not None:
                                                    await on_status("二维码已过期，已自动刷新，请扫描新二维码：")
                                                await on_qr_ready(new_qr)
                                            except Exception:
                                                pass

                                    await asyncio.sleep(max(0.05, float(poll_interval_sec)))

                                raise WeiboLoginTimeoutError("微博扫码登录超时，请重新发送 /weibo_login_scan")
                            finally:
                                await page.close()
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
            finally:
                self._session = None
                for path in temp_qr_files:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

    async def login_by_code(
        self,
        *,
        account: str,
        password: str,
        owner_umo: str,
        on_code_requested: Callable[[str], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]] | None = None,
        timeout_sec: float = 180.0,
        poll_interval_sec: float = 0.8,
        user_agent: str = WEIBO_LOGIN_USER_AGENT,
    ) -> WeiboLoginResult:
        """Log in via configured account/password and interactive verification code."""
        account = str(account or "").strip()
        password = str(password or "").strip()
        if not account:
            raise WeiboInteractiveLoginError(
                "未配置微博登录账号，请先在后台 weibo_settings.login_account 中填写账号/手机号"
            )
        if self._lock.locked():
            raise WeiboInteractiveLoginError(
                "当前已有正在进行的微博登录任务，请先完成或发送 /weibo_login_cancel 取消"
            )

        async with self._lock:
            session = _ActiveLoginSession(mode="code", owner_umo=owner_umo)
            self._session = session
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:
                self._session = None
                raise WeiboInteractiveLoginError(
                    "Playwright 不可用，无法执行微博验证码登录"
                ) from exc

            configure_playwright_browser_path()
            try:
                async with async_playwright() as playwright:
                    browser = await launch_chromium(
                        playwright,
                        headless=True,
                        fallback_executable_paths=browser_channel_candidates(),
                    )
                    try:
                        context = await browser.new_context(
                            viewport={"width": 1280, "height": 900},
                            locale="zh-CN",
                            user_agent=user_agent,
                        )
                        try:
                            page = await context.new_page()
                            try:
                                await page.goto(
                                    WEIBO_SIGNIN_URL,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=6000)
                                except Exception:
                                    pass

                                if await _detect_captcha_challenge(page):
                                    raise WeiboLoginCaptchaBlockedError(
                                        "微博登录页触发了滑块/图形安全验证，无法自动绕过；请改用 /weibo_login_scan 扫码登录"
                                    )

                                used_sms_tab = False
                                if password:
                                    pwd_tab = await _find_first_visible(
                                        page, _PASSWORD_TAB_SELECTORS
                                    )
                                    if pwd_tab is not None:
                                        try:
                                            await pwd_tab.click()
                                            await asyncio.sleep(0.5)
                                        except Exception:
                                            pass

                                pwd_input = (
                                    await _find_first_visible(page, _PASSWORD_INPUT_SELECTORS)
                                    if password
                                    else None
                                )
                                acct_input = await _find_first_visible(
                                    page, _ACCOUNT_INPUT_SELECTORS
                                )

                                if pwd_input is not None and acct_input is not None:
                                    await acct_input.fill(account)
                                    await pwd_input.fill(password)
                                    submit_btn = await _find_first_visible(
                                        page, _SUBMIT_BUTTON_SELECTORS
                                    )
                                    if submit_btn is None:
                                        raise WeiboInteractiveLoginError("未找到微博登录提交按钮")
                                    await submit_btn.click()
                                else:
                                    # Fallback to SMS code login tab when password input is unavailable.
                                    sms_tab = await _find_first_visible(
                                        page, _SMS_TAB_SELECTORS
                                    )
                                    if sms_tab is not None:
                                        try:
                                            await sms_tab.click()
                                            await asyncio.sleep(0.5)
                                        except Exception:
                                            pass
                                    acct_input = await _find_first_visible(
                                        page, _ACCOUNT_INPUT_SELECTORS
                                    )
                                    if acct_input is None:
                                        raise WeiboInteractiveLoginError(
                                            "未找到微博账号/手机号输入框，请改用 /weibo_login_scan 扫码登录"
                                        )
                                    await acct_input.fill(account)
                                    used_sms_tab = True

                                # Wait for transition: direct login, captcha block, error, or code verification.
                                code_prompt_ready = used_sms_tab
                                transition_deadline = time.monotonic() + 15.0
                                while not code_prompt_ready and time.monotonic() < transition_deadline:
                                    if session.cancel_event.is_set():
                                        raise WeiboLoginCancelledError("微博验证码登录已取消")
                                    if await _is_logged_in(page, context):
                                        return await _collect_final_login_result(page, context)
                                    if await _detect_captcha_challenge(page):
                                        raise WeiboLoginCaptchaBlockedError(
                                            "微博账密登录触发了滑块/图形安全验证，无法自动绕过；请改用 /weibo_login_scan 扫码登录"
                                        )
                                    if (
                                        await _find_first_visible(page, _CODE_INPUT_SELECTORS)
                                        is not None
                                        or await _find_first_visible(
                                            page, _SEND_CODE_BUTTON_SELECTORS
                                        )
                                        is not None
                                    ):
                                        code_prompt_ready = True
                                        break
                                    page_error = await _detect_page_error_text(page)
                                    if page_error:
                                        raise WeiboInteractiveLoginError(
                                            f"微博登录失败：{page_error}"
                                        )
                                    await asyncio.sleep(max(0.05, float(poll_interval_sec)))

                                if await _is_logged_in(page, context):
                                    return await _collect_final_login_result(page, context)

                                if not code_prompt_ready:
                                    raise WeiboInteractiveLoginError(
                                        "微博未进入验证码验证页面且未完成登录，请改用 /weibo_login_scan 扫码登录"
                                    )

                                # Trigger SMS / verification code sending if a send button is present.
                                send_btn = await _find_first_visible(
                                    page, _SEND_CODE_BUTTON_SELECTORS
                                )
                                if send_btn is not None:
                                    try:
                                        await send_btn.click()
                                        await asyncio.sleep(1.5)
                                    except Exception as exc:
                                        logger.debug("点击发送验证码按钮异常: %s", exc)
                                    if await _detect_captcha_challenge(page):
                                        raise WeiboLoginCaptchaBlockedError(
                                            "发送验证码前触发了滑块/图形安全验证，无法自动绕过；请改用 /weibo_login_scan 扫码登录"
                                        )
                                    send_error = await _detect_page_error_text(page)
                                    if send_error and "频繁" in send_error:
                                        raise WeiboInteractiveLoginError(
                                            f"发送验证码失败：{send_error}"
                                        )

                                max_code_attempts = 2
                                for attempt in range(max_code_attempts):
                                    prompt = (
                                        "已触发微博验证码发送，请在 3 分钟内发送验证码"
                                        "（直接回复数字验证码，或使用 /weibo_code 验证码；发送 /weibo_login_cancel 可取消）。"
                                        if attempt == 0
                                        else "验证码校验未通过，请重新发送验证码（或发送 /weibo_login_cancel 取消）："
                                    )
                                    await on_code_requested(prompt)
                                    code = await self._wait_for_code(session, timeout_sec)

                                    code_input = await _find_first_visible(
                                        page, _CODE_INPUT_SELECTORS
                                    )
                                    if code_input is None:
                                        raise WeiboInteractiveLoginError(
                                            "未找到验证码输入框，登录页面可能已失效"
                                        )
                                    await code_input.fill(code)
                                    confirm_btn = await _find_first_visible(
                                        page, _SUBMIT_BUTTON_SELECTORS
                                    )
                                    if confirm_btn is None:
                                        raise WeiboInteractiveLoginError("未找到验证码提交按钮")
                                    await confirm_btn.click()

                                    verify_deadline = time.monotonic() + 20.0
                                    wrong_code_error = None
                                    while time.monotonic() < verify_deadline:
                                        if session.cancel_event.is_set():
                                            raise WeiboLoginCancelledError("微博验证码登录已取消")
                                        if await _is_logged_in(page, context):
                                            return await _collect_final_login_result(
                                                page, context
                                            )
                                        if await _detect_captcha_challenge(page):
                                            raise WeiboLoginCaptchaBlockedError(
                                                "验证码提交后触发了滑块/图形安全验证，请改用 /weibo_login_scan 扫码登录"
                                            )
                                        page_error = await _detect_page_error_text(page)
                                        if page_error:
                                            if "验证码" in page_error:
                                                wrong_code_error = page_error
                                                break
                                            raise WeiboInteractiveLoginError(
                                                f"微博登录失败：{page_error}"
                                            )
                                        await asyncio.sleep(max(0.05, float(poll_interval_sec)))

                                    if wrong_code_error and attempt < max_code_attempts - 1:
                                        if on_status is not None:
                                            await on_status(f"⚠️ {wrong_code_error}")
                                        continue
                                    if wrong_code_error:
                                        raise WeiboInteractiveLoginError(
                                            f"微博登录失败：{wrong_code_error}"
                                        )

                                raise WeiboLoginTimeoutError(
                                    "提交验证码后未检测到登录完成，请检查验证码或改用 /weibo_login_scan 扫码登录"
                                )
                            finally:
                                await page.close()
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
            finally:
                self._session = None


__all__ = [
    "WEIBO_HOME_URL",
    "WEIBO_LOGIN_USER_AGENT",
    "WEIBO_SIGNIN_URL",
    "WeiboInteractiveLoginError",
    "WeiboInteractiveLoginManager",
    "WeiboLoginCancelledError",
    "WeiboLoginCaptchaBlockedError",
    "WeiboLoginResult",
    "WeiboLoginTimeoutError",
]
