# xhs-mcp request cache
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# File-based JSON cache with per-tool TTL.
# Cache key = md5(tool_name + sorted JSON of relevant params)
# File format: {"cached_at": "ISO", "ttl_seconds": N, "data": {...}}

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("xhs-mcp")

# TTL per tool (seconds)
TTL_MAP = {
    "xhs_search": 15 * 60,       # 15 minutes
    "xhs_detail": 24 * 60 * 60,  # 24 hours
    "xhs_creator": 7 * 24 * 60 * 60,  # 7 days
}

# Tools that should never be cached
NO_CACHE_TOOLS = {"xhs_login", "xhs_status"}


def _cache_dir() -> str:
    """Return the absolute cache directory path, creating it if needed."""
    d = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _make_key(tool_name: str, params: Dict[str, Any]) -> str:
    """Generate a cache key from tool name + params.

    Uses md5 hash of the canonical JSON representation.
    """
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw = f"{tool_name}:{canonical}"
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"{tool_name}_{h}"


def _cache_path(key: str) -> str:
    return os.path.join(_cache_dir(), f"{key}.json")


def get(tool_name: str, params: Dict[str, Any]) -> Optional[Dict]:
    """Read from cache if valid entry exists.

    Returns the cached data dict, or None if miss/expired.
    """
    if tool_name in NO_CACHE_TOOLS:
        return None

    key = _make_key(tool_name, params)
    path = _cache_path(key)

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)

        cached_at = entry.get("cached_at", "")
        ttl = entry.get("ttl_seconds", 0)

        # Parse cached_at ISO timestamp
        cached_ts = datetime.fromisoformat(cached_at).timestamp()
        now = time.time()

        if now - cached_ts > ttl:
            # Expired — remove stale file
            logger.info(f"Cache expired for {key}, removing.")
            try:
                os.remove(path)
            except OSError:
                pass
            return None

        logger.info(f"Cache hit for {key} (age: {int(now - cached_ts)}s, ttl: {ttl}s)")
        return entry.get("data")

    except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
        logger.warning(f"Cache read error for {key}: {e}")
        # Remove corrupt cache file
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def put(tool_name: str, params: Dict[str, Any], data: Dict) -> None:
    """Write data to cache."""
    if tool_name in NO_CACHE_TOOLS:
        return

    ttl = TTL_MAP.get(tool_name)
    if ttl is None:
        return

    key = _make_key(tool_name, params)
    path = _cache_path(key)

    entry = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": ttl,
        "data": data,
    }

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, separators=(",", ":"))
        logger.info(f"Cached {key} (ttl={ttl}s)")
    except OSError as e:
        logger.warning(f"Cache write error for {key}: {e}")


def get_stats() -> Dict[str, Any]:
    """Return cache statistics for xhs_status."""
    cache_d = _cache_dir()
    entries = 0
    total_bytes = 0

    try:
        for fname in os.listdir(cache_d):
            if fname.endswith(".json"):
                entries += 1
                fpath = os.path.join(cache_d, fname)
                try:
                    total_bytes += os.path.getsize(fpath)
                except OSError:
                    pass
    except OSError:
        pass

    return {
        "cache_entries": entries,
        "cache_size_mb": round(total_bytes / (1024 * 1024), 2),
    }


def clear_all() -> int:
    """Remove all cache files. Returns count of files removed."""
    cache_d = _cache_dir()
    removed = 0
    try:
        for fname in os.listdir(cache_d):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(cache_d, fname))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed
