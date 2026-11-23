"""Distinguish an explicit Cookie replacement from a stale saved configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..common.cookies import (
    atomic_write_cookie_text,
    serialize_cookie_header,
    serialize_netscape_cookies,
)

WEIBO_SCOPED_DOMAINS = frozenset(
    {
        "weibo.com",
        "www.weibo.com",
        "passport.weibo.com",
        "sina.com.cn",
        "login.sina.com.cn",
    }
)


def is_desktop_weibo_domain(domain: str) -> bool:
    """Return True only for cookies sent directly to weibo.com."""
    return str(domain or "").strip().lower().rstrip(".").lstrip(".") == "weibo.com"


def filter_weibo_browser_cookies(
    browser_cookies: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only valid Weibo / Sina SSO cookies with domain/path scoping."""
    filtered: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in browser_cookies:
        domain = str(raw.get("domain") or "").strip().lower()
        clean_domain = domain.rstrip(".").lstrip(".")
        if clean_domain not in WEIBO_SCOPED_DOMAINS:
            continue
        path = str(raw.get("path") or "/")
        if not path.startswith("/"):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not name or not value:
            continue
        if any(
            ch.isspace() or ord(ch) < 32 or ord(ch) == 127
            for ch in (*name, *value)
        ):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": bool(raw.get("secure")),
            "httpOnly": bool(raw.get("httpOnly")),
        }
        expires = raw.get("expires")
        try:
            expires_int = int(float(expires)) if expires is not None else 0
        except (TypeError, ValueError, OverflowError):
            expires_int = 0
        if expires_int > 0:
            entry["expires"] = expires_int
        filtered[(domain, path, name)] = entry
    return list(filtered.values())


def update_cookie_source(
    existing_seeds: Sequence[Mapping[str, Any]],
    desktop_cookies: Mapping[str, str],
    browser_cookies: Sequence[Mapping[str, Any]] | None = None,
    *,
    force_netscape: bool = False,
) -> str:
    """Preserve SSO domain cookies in Netscape format when available.

    - If Playwright ``browser_cookies`` include non-desktop/SSO domains (such as
      ``login.sina.com.cn`` / ``sina.com.cn``), or ``existing_seeds`` is already
      Netscape-scoped, or ``force_netscape`` is True, serialize all allowed
      Weibo + Sina SSO cookies into Netscape format so ``tgc`` and domain scope
      are preserved on disk and shared cleanly with consumers.
    - Otherwise, serialize as a Cookie header for simple header-only configs.
    """
    if browser_cookies is not None:
        scoped = filter_weibo_browser_cookies(browser_cookies)
        has_sso = any(
            not is_desktop_weibo_domain(str(cookie.get("domain") or ""))
            for cookie in scoped
        )
        if scoped and (force_netscape or has_sso or existing_seeds):
            return serialize_netscape_cookies(scoped)
    if existing_seeds:
        retained = [
            dict(seed)
            for seed in existing_seeds
            if not is_desktop_weibo_domain(str(seed.get("domain") or ""))
        ]
        for name, value in desktop_cookies.items():
            if name and value:
                retained.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".weibo.com",
                        "path": "/",
                        "secure": True,
                    }
                )
        if retained:
            return serialize_netscape_cookies(retained)
    return serialize_cookie_header(desktop_cookies)


def compute_sso_source_digest(
    scoped_cookies: Sequence[Mapping[str, Any]],
) -> str | None:
    """Hash normalized non-desktop SSO cookies for browser state matching."""
    sso_items = [
        {
            k: (int(float(c[k])) if k == "expires" else c[k])
            for k in ("name", "value", "domain", "path", "secure", "httpOnly", "expires")
            if k in c and (k != "expires" or float(c[k]) > 0)
        }
        for c in scoped_cookies
        if not is_desktop_weibo_domain(str(c.get("domain") or ""))
    ]
    if not sso_items:
        return None
    return hashlib.sha256(json.dumps(sso_items, sort_keys=True).encode()).hexdigest()


def save_browser_storage_state(
    state_path: Path,
    *,
    desktop_cookies: Mapping[str, str],
    browser_cookies: Sequence[Mapping[str, Any]],
    storage_state: Mapping[str, Any],
    session_cookie_names: Sequence[str],
) -> None:
    """Persist Playwright storage state compatible with ``collect_browser_cookies``."""
    selected = {name: desktop_cookies.get(name, "") for name in session_cookie_names}
    digest = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    scoped = filter_weibo_browser_cookies(browser_cookies)
    sso_digest = compute_sso_source_digest(scoped)
    full_source_digest = (
        hashlib.sha256(json.dumps(scoped, sort_keys=True).encode()).hexdigest()
        if scoped
        else None
    )
    atomic_write_cookie_text(
        state_path,
        json.dumps(
            {
                "input_digest": digest,
                "output_digest": digest,
                "source_digest": full_source_digest,
                "output_source_digest": sso_digest,
                "storage_state": dict(storage_state),
            }
        ),
    )


def _fingerprint(cookie: str) -> str:
    return hashlib.sha256(cookie.strip().encode("utf-8")).hexdigest()


def _source_path(path: Path) -> Path:
    return path.with_name(path.name + ".source.json")


def _read_source(path: Path) -> dict:
    try:
        data = json.loads(_source_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_cookie_source(configured: str, path: Path) -> str:
    """Use refreshed values only when the configured input is already known.

    A new nonempty input always replaces the file, including on first upgrade
    from versions that did not record the configuration fingerprint.
    The sidecar stores hashes only, never another copy of the credentials.
    """
    configured = configured.strip()
    persisted = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not configured:
        return persisted
    fingerprint = _fingerprint(configured)
    source = _read_source(path)
    if persisted and fingerprint in (source.get("configured"), source.get("refreshed")):
        return persisted
    atomic_write_cookie_text(path, configured)
    atomic_write_cookie_text(
        _source_path(path), json.dumps({"configured": fingerprint})
    )
    return configured


def record_refreshed_source(cookie: str, path: Path) -> None:
    """Remember the automatic value also exposed by the mutable config object."""
    source = _read_source(path)
    source["refreshed"] = _fingerprint(cookie)
    atomic_write_cookie_text(_source_path(path), json.dumps(source))
