from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from .paths import get_playwright_browsers_path

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

_INSTALL_LOCK = asyncio.Lock()
_INSTALL_ATTEMPTED = False


def configure_playwright_browser_path() -> Path:
    browsers_path = get_playwright_browsers_path()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    return browsers_path


async def launch_chromium(
    playwright,
    *,
    headless: bool = True,
    fallback_executable_paths: Iterable[str | None] = (),
):
    configure_playwright_browser_path()
    first_exc: Exception | None = None
    try:
        return await playwright.chromium.launch(headless=headless)
    except Exception as exc:
        first_exc = exc

    if await ensure_chromium_installed():
        try:
            return await playwright.chromium.launch(headless=headless)
        except Exception as exc:
            first_exc = exc

    for candidate in fallback_executable_paths:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return await playwright.chromium.launch(
                headless=headless,
                executable_path=str(path),
            )
        except Exception:
            continue

    if first_exc is not None:
        raise first_exc
    raise RuntimeError("Chromium launch failed")


async def ensure_chromium_installed() -> bool:
    global _INSTALL_ATTEMPTED
    async with _INSTALL_LOCK:
        if _INSTALL_ATTEMPTED:
            return False
        _INSTALL_ATTEMPTED = True
        browsers_path = configure_playwright_browser_path()
        logger.info("Installing Playwright Chromium into %s", browsers_path)
        return await asyncio.to_thread(_install_chromium, browsers_path)


def _install_chromium(browsers_path: Path) -> bool:
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except Exception as exc:
        logger.warning("Playwright Chromium install failed: %s", exc)
        return False
    if result.returncode == 0:
        return True
    output = (result.stdout or "").strip()
    if len(output) > 1200:
        output = output[-1200:]
    logger.warning(
        "Playwright Chromium install exited with %s: %s",
        result.returncode,
        output,
    )
    return False


def browser_channel_candidates() -> list[str | None]:
    return [
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
