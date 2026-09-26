"""Cookie parsing, response capture and atomic persistence helpers."""

from __future__ import annotations

import os
import tempfile
import time
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterable, Mapping


def iter_set_cookie_headers(headers: object) -> list[str]:
    """Return all Set-Cookie header values from httpx or aiohttp headers."""
    if headers is None:
        return []
    try:
        values = headers.get_list("set-cookie")
        if values:
            return [str(value) for value in values]
    except (AttributeError, TypeError):
        pass
    try:
        values = headers.getall("Set-Cookie", [])
        if values:
            return [str(value) for value in values]
    except (AttributeError, TypeError):
        pass
    try:
        value = headers.get("set-cookie") or headers.get("Set-Cookie")
    except AttributeError:
        value = None
    return [str(value)] if value else []


def _is_safe_cookie_pair(name: str, value: str) -> bool:
    if not name or not value:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def host_matches(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".").lstrip(".")
    if not host:
        return False
    for allowed in allowed_hosts:
        allowed = str(allowed).lower().rstrip(".").lstrip(".")
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def parse_set_cookie_updates(
    headers: object,
    *,
    response_host: str,
    allowed_hosts: Iterable[str],
) -> dict[str, str | None]:
    """Parse safe, same-site Set-Cookie updates."""
    if not host_matches(response_host, allowed_hosts):
        return {}
    updates: dict[str, str | None] = {}
    for item in iter_set_cookie_headers(headers):
        parsed = SimpleCookie()
        try:
            parsed.load(item)
        except Exception:
            continue
        for name, morsel in parsed.items():
            cookie_domain = (morsel["domain"] or "").strip()
            if cookie_domain and not host_matches(cookie_domain, allowed_hosts):
                continue
            value = morsel.value.strip()
            max_age = (morsel["max-age"] or "").strip().lower()
            expires = (morsel["expires"] or "").strip()
            deleted = max_age in {"0", "-1"} or (
                not value and expires and _is_expired(expires)
            )
            if not _is_safe_cookie_pair(name, value):
                if not value and not any(
                    ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in name
                ):
                    updates[name] = None
                continue
            updates[name] = None if deleted else value
    return updates


def _is_expired(value: str) -> bool:
    try:
        return parsedate_to_datetime(value).timestamp() <= time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def merge_cookie_updates(
    cookies: dict[str, str], updates: Mapping[str, str | None]
) -> bool:
    changed = False
    for name, value in updates.items():
        if value is None:
            changed = cookies.pop(name, None) is not None or changed
        elif cookies.get(name) != value:
            cookies[name] = value
            changed = True
    return changed


def serialize_cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def atomic_write_cookie_text(path: str | Path, text: str) -> None:
    """Write a cookie file without exposing a partially written session."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text.rstrip("\n"))
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def serialize_netscape_cookies(cookies: Iterable[Mapping[str, object]]) -> str:
    """Serialize Playwright cookie dictionaries to cookies.txt format."""
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").strip()
        if not name or not domain or not _is_safe_cookie_pair(name, value):
            continue
        http_only = bool(cookie.get("httpOnly"))
        if http_only and not domain.startswith("#HttpOnly_"):
            domain = f"#HttpOnly_{domain}"
        clean_domain = domain.removeprefix("#HttpOnly_")
        include_subdomains = "TRUE" if clean_domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires")
        try:
            expires_text = str(int(float(expires))) if expires else "0"
        except (TypeError, ValueError, OverflowError):
            expires_text = "0"
        lines.append(
            "\t".join(
                [domain, include_subdomains, path, secure, expires_text, name, value]
            )
        )
    return "\n".join(lines)


__all__ = [
    "atomic_write_cookie_text",
    "host_matches",
    "iter_set_cookie_headers",
    "merge_cookie_updates",
    "parse_set_cookie_updates",
    "serialize_cookie_header",
    "serialize_netscape_cookies",
]
