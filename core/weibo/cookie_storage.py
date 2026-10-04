"""Distinguish an explicit Cookie replacement from a stale saved configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..common.cookies import atomic_write_cookie_text


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
