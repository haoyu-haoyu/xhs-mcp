"""Unit tests for xhs/cache.py (deterministic file-based TTL cache)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xhs import cache as request_cache


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Redirect the cache directory to a pytest tmp_path so real cache is untouched."""
    monkeypatch.setattr(request_cache, "_cache_dir", lambda: str(tmp_path))
    # Ensure tool "xhs_search" has a known TTL for the tests below.
    monkeypatch.setitem(request_cache.TTL_MAP, "xhs_search", 900)
    yield tmp_path


def test_get_returns_none_on_miss():
    assert request_cache.get("xhs_search", {"k": "absent"}) is None


def test_put_then_get_roundtrip():
    params = {"keywords": ["猫"]}
    request_cache.put("xhs_search", params, {"notes": []})

    got = request_cache.get("xhs_search", params)
    assert got == {"notes": []}


def test_put_skips_no_cache_tools():
    request_cache.put("xhs_login", {"action": "check"}, {"status": "valid"})
    assert request_cache.get("xhs_login", {"action": "check"}) is None


def test_expired_entry_returns_none_and_deletes_file(isolated_cache_dir):
    params = {"keywords": ["狗"]}

    # Write an already-expired entry by hand.
    key = request_cache._make_key("xhs_search", params)
    path = Path(request_cache._cache_path(key))
    expired_entry = {
        "cached_at": datetime.fromtimestamp(
            time.time() - 10_000, tz=timezone.utc
        ).isoformat(),
        "ttl_seconds": 60,
        "data": {"old": True},
    }
    path.write_text(json.dumps(expired_entry), encoding="utf-8")
    assert path.exists()

    assert request_cache.get("xhs_search", params) is None
    # Corrupt / expired files should be removed proactively.
    assert not path.exists()


def test_corrupt_cache_file_removed(isolated_cache_dir):
    params = {"keywords": ["乱码"]}
    key = request_cache._make_key("xhs_search", params)
    path = Path(request_cache._cache_path(key))
    path.write_text("this is not json", encoding="utf-8")

    assert request_cache.get("xhs_search", params) is None
    assert not path.exists()


def test_keys_are_order_independent():
    """Cache key should be stable regardless of dict ordering."""
    k1 = request_cache._make_key("xhs_search", {"a": 1, "b": 2})
    k2 = request_cache._make_key("xhs_search", {"b": 2, "a": 1})
    assert k1 == k2


def test_put_and_get_handle_unicode_params():
    params = {"keywords": ["伦敦租房", "KCL"]}
    request_cache.put("xhs_search", params, {"ok": True})

    got = request_cache.get("xhs_search", params)
    assert got == {"ok": True}


def test_get_stats_reflects_written_entries(isolated_cache_dir):
    request_cache.put("xhs_search", {"a": 1}, {"x": 1})
    request_cache.put("xhs_search", {"a": 2}, {"x": 2})

    stats = request_cache.get_stats()
    assert stats["cache_entries"] == 2
    assert stats["cache_size_mb"] >= 0


def test_clear_all_removes_all_entries(isolated_cache_dir):
    request_cache.put("xhs_search", {"a": 1}, {"x": 1})
    request_cache.put("xhs_search", {"a": 2}, {"x": 2})

    removed = request_cache.clear_all()
    assert removed == 2
    assert request_cache.get_stats()["cache_entries"] == 0


def test_put_with_unknown_tool_is_noop():
    """Tools without a TTL mapping should silently skip caching."""
    request_cache.put("unknown_tool", {"k": 1}, {"v": 1})
    # Nothing written → stats is zero entries.
    assert request_cache.get_stats()["cache_entries"] == 0
