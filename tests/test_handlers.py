"""Unit tests for xhs.handlers — the MCP tool business logic.

These tests swap in a fake BrowserManager + in-memory cache via
HandlerContext so we exercise the handlers without a real browser or
network.  This is only possible because #4 extracted the handlers out
of server.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from xhs import handlers


# ── Test doubles ──────────────────────────────────────────────────────


class FakeCache:
    """In-memory stand-in for ``xhs.cache`` — just enough for the handlers."""

    def __init__(self):
        self.store: Dict[str, Dict] = {}

    @staticmethod
    def _key(tool_name: str, params: dict) -> str:
        return tool_name + ":" + json.dumps(params, sort_keys=True, ensure_ascii=False)

    def get(self, tool_name: str, params: dict):
        return self.store.get(self._key(tool_name, params))

    def put(self, tool_name: str, params: dict, data: dict):
        self.store[self._key(tool_name, params)] = data

    def get_stats(self):
        return {"cache_entries": len(self.store), "cache_size_mb": 0.0}


def _make_ctx(client: Any, cache: FakeCache | None = None):
    """Build a HandlerContext that never touches a real browser."""
    cache = cache or FakeCache()
    browser_mgr = SimpleNamespace(is_started=True, cookie_dict={"a1": "x"})
    return handlers.HandlerContext(
        browser_mgr=browser_mgr,
        client_factory=lambda: client,
        cache_module=cache,
    )


def _text_payload(response) -> dict:
    """Unwrap a handler's MCP response back into a dict."""
    assert len(response) == 1
    return json.loads(response[0].text)


# ── xhs_search ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_requires_keywords():
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_search(ctx, {"keywords": []})
    payload = _text_payload(resp)
    assert payload["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_search_happy_path_formats_and_caches():
    client = AsyncMock()
    client.search_notes.return_value = {
        "has_more": False,
        "items": [
            {
                "id": "n1",
                "xsec_token": "tok",
                "xsec_source": "src",
                "note_card": {
                    "display_title": "hello",
                    "desc": "d",
                    "user": {"user_id": "u1", "nickname": "nick"},
                    "interact_info": {"liked_count": "10"},
                },
            },
            # Non-note items should be filtered out:
            {"model_type": "rec_query", "id": "skip"},
        ],
    }
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    resp = await handlers.handle_xhs_search(ctx, {"keywords": ["cat"]})
    payload = _text_payload(resp)

    assert payload["total"] == 1
    assert payload["notes"][0]["note_id"] == "n1"
    assert payload["notes"][0]["title"] == "hello"
    # Cache should have been populated so a second call returns cached data.
    cached_resp = await handlers.handle_xhs_search(ctx, {"keywords": ["cat"]})
    cached_payload = _text_payload(cached_resp)
    assert cached_payload["_from_cache"] is True
    # Underlying client should NOT have been called a second time.
    assert client.search_notes.call_count == 1


@pytest.mark.asyncio
async def test_search_tolerates_null_nested_fields():
    """Regression: XHS occasionally returns `note_card: null`; the handler
    must skip / degrade gracefully rather than raising AttributeError
    and aborting the whole keyword search."""
    client = AsyncMock()
    client.search_notes.return_value = {
        "has_more": False,
        "items": [
            {"id": "n1", "note_card": None},      # null nested dict
            {"id": "n2", "note_card": {"display_title": "ok"}},
        ],
    }
    ctx = _make_ctx(client=client)

    resp = await handlers.handle_xhs_search(ctx, {"keywords": ["cat"]})
    payload = _text_payload(resp)
    assert payload["total"] == 2  # both items formatted, neither crashed
    assert payload["notes"][0]["title"] == ""  # degraded gracefully
    assert payload["notes"][1]["title"] == "ok"


@pytest.mark.asyncio
async def test_search_per_keyword_error_reported_in_summary():
    client = AsyncMock()
    # First keyword raises; second succeeds → partial success.
    import httpx

    async def fake_search(keyword, page, sort, note_type):
        if keyword == "bad":
            raise httpx.ConnectError("boom")
        return {"has_more": False, "items": []}

    client.search_notes.side_effect = fake_search
    ctx = _make_ctx(client=client)

    resp = await handlers.handle_xhs_search(ctx, {"keywords": ["bad", "good"]})
    payload = _text_payload(resp)
    assert payload["keyword_results"]["bad"]["count"] == 0
    assert "ConnectError" in payload["keyword_results"]["bad"]["error"]
    assert payload["keyword_results"]["good"]["count"] == 0  # empty items


@pytest.mark.asyncio
async def test_search_does_not_cache_partial_failure():
    """Regression: a keyword failing must not poison the 15-min cache for
    the whole multi-keyword request on subsequent calls."""
    import httpx

    client = AsyncMock()

    async def fake_search(keyword, page, sort, note_type):
        if keyword == "bad":
            raise httpx.ConnectError("boom")
        return {"has_more": False, "items": []}

    client.search_notes.side_effect = fake_search
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    await handlers.handle_xhs_search(ctx, {"keywords": ["bad", "good"]})
    # Nothing should have been persisted because "bad" failed.
    assert cache.store == {}


@pytest.mark.asyncio
async def test_search_cache_respects_keyword_order():
    """Regression: ["a","b"] and ["b","a"] must NOT share a cache entry,
    because the response `notes` list preserves caller-specified order.
    If they aliased, whichever input hit the cache miss first would pin
    the ordering for the other."""
    client = AsyncMock()

    async def fake_search(keyword, page, sort, note_type):
        return {
            "has_more": False,
            "items": [{"id": keyword, "note_card": {"display_title": keyword}}],
        }

    client.search_notes.side_effect = fake_search
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    r1 = await handlers.handle_xhs_search(ctx, {"keywords": ["a", "b"]})
    titles_1 = [n["title"] for n in _text_payload(r1)["notes"]]
    r2 = await handlers.handle_xhs_search(ctx, {"keywords": ["b", "a"]})
    titles_2 = [n["title"] for n in _text_payload(r2)["notes"]]
    # Orders must differ — the second call must NOT be served from the
    # first call's cache entry (would have returned ["a","b"]).
    assert titles_1 == ["a", "b"]
    assert titles_2 == ["b", "a"]


@pytest.mark.asyncio
async def test_search_deduplicates_keywords():
    """Regression: duplicate keywords must collapse to a single search so
    keyword_results isn't corrupted by overwrite, and we don't pay the
    5-10s inter-keyword sleep for no reason."""
    client = AsyncMock()
    client.search_notes.return_value = {"has_more": False, "items": []}
    ctx = _make_ctx(client=client)

    resp = await handlers.handle_xhs_search(ctx, {"keywords": ["dup", "dup", "other"]})
    payload = _text_payload(resp)
    assert set(payload["keyword_results"].keys()) == {"dup", "other"}
    # Only two underlying searches performed, not three.
    assert client.search_notes.call_count == 2


# ── xhs_detail ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_requires_matching_tokens_and_ids():
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_detail(
        ctx, {"note_ids": ["a", "b"], "xsec_tokens": ["t1"]}
    )
    payload = _text_payload(resp)
    assert payload["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_detail_skips_empty_response_and_marks_failure():
    client = AsyncMock()
    client.get_note_by_id.return_value = None
    ctx = _make_ctx(client=client)

    resp = await handlers.handle_xhs_detail(
        ctx, {"note_ids": ["n1"], "xsec_tokens": ["t1"]}
    )
    payload = _text_payload(resp)
    assert payload["summary"]["succeeded"] == 0
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["failed_details"][0]["error"] == "empty_response"


@pytest.mark.asyncio
async def test_detail_does_not_cache_on_comments_fetch_failure():
    """Regression: if get_comments=True and the comments fetch fails, the
    partial payload carrying comments_error must not be cached — otherwise
    the empty comments would be served for the full 24h TTL."""
    import httpx

    client = AsyncMock()
    client.get_note_by_id.return_value = {
        "note_id": "n1",
        "display_title": "t",
        "user": {},
        "interact_info": {},
    }
    client.get_note_all_comments.side_effect = httpx.ConnectError("boom")
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    await handlers.handle_xhs_detail(
        ctx,
        {"note_ids": ["n1"], "xsec_tokens": ["t1"], "get_comments": True},
    )
    assert cache.store == {}


@pytest.mark.asyncio
async def test_detail_does_not_cache_on_sub_comment_partial_failure():
    """Regression: sub-comment pagination failure marks the parent comment
    with _sub_comments_partial; the detail handler must not cache that
    truncated tree for the 24h TTL."""
    client = AsyncMock()
    client.get_note_by_id.return_value = {
        "note_id": "n1",
        "display_title": "t",
        "user": {},
        "interact_info": {},
    }
    # Simulate the partial-failure marker that get_note_all_comments
    # attaches when a sub-comment page raises mid-pagination.
    client.get_note_all_comments.return_value = [
        {"id": "c1", "content": "top", "user_info": {}, "_sub_comments_partial": True},
    ]
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    await handlers.handle_xhs_detail(
        ctx,
        {"note_ids": ["n1"], "xsec_tokens": ["t1"], "get_comments": True},
    )
    assert cache.store == {}


@pytest.mark.asyncio
async def test_detail_cache_hit_skips_network():
    client = AsyncMock()
    cache = FakeCache()
    cache.put(
        "xhs_detail",
        {"note_id": "n1", "get_comments": False, "comment_count": 20},
        {"note_id": "n1", "cached": True},
    )
    ctx = _make_ctx(client=client, cache=cache)

    resp = await handlers.handle_xhs_detail(
        ctx, {"note_ids": ["n1"], "xsec_tokens": ["t1"]}
    )
    payload = _text_payload(resp)
    assert payload["summary"]["from_cache"] == 1
    assert payload["summary"]["succeeded"] == 1
    client.get_note_by_id.assert_not_called()


# ── xhs_creator ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creator_requires_user_ids():
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_creator(ctx, {"user_ids": []})
    payload = _text_payload(resp)
    assert payload["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_creator_profile_then_notes_populates_expected_fields():
    client = AsyncMock()
    client.get_creator_info.return_value = {
        "basicInfo": {"nickname": "N", "desc": "D", "imageb": "img"},
        "interactions": [
            {"name": "粉丝", "count": "100"},
            {"name": "关注", "count": "5"},
        ],
        "tags": [{"name": "Food"}, "Travel"],
    }
    client.get_creator_notes.return_value = [
        {"note_id": "n1", "display_title": "T1", "time": 123, "xsec_token": "tok"},
    ]
    ctx = _make_ctx(client=client)

    resp = await handlers.handle_xhs_creator(ctx, {"user_ids": ["u1"], "note_count": 1})
    payload = _text_payload(resp)
    c = payload["creators"][0]
    assert c["nickname"] == "N"
    assert c["fans"] == "100"
    assert c["follows"] == "5"
    assert c["tags"] == ["Food", "Travel"]
    assert c["recent_notes"][0]["note_id"] == "n1"


@pytest.mark.asyncio
async def test_creator_does_not_cache_on_notes_fetch_failure():
    """Regression: if profile fetch succeeds but notes fetch fails, the
    partial creator payload (with notes_error) must NOT be cached,
    otherwise incomplete data sticks for the 7-day TTL."""
    import httpx

    client = AsyncMock()
    client.get_creator_info.return_value = {
        "basicInfo": {"nickname": "N"},
        "interactions": [],
    }
    client.get_creator_notes.side_effect = httpx.ConnectError("notes boom")
    cache = FakeCache()
    ctx = _make_ctx(client=client, cache=cache)

    resp = await handlers.handle_xhs_creator(
        ctx, {"user_ids": ["u1"], "note_count": 1}
    )
    payload = _text_payload(resp)
    # Response still returns what we have (graceful degrade).
    assert payload["creators"][0]["nickname"] == "N"
    assert "notes_error" in payload["creators"][0]
    # But cache must be empty — a fresh call later should retry.
    assert cache.store == {}


# ── xhs_login ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_rejects_unknown_action():
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_login(ctx, {"action": "bogus"})
    payload = _text_payload(resp)
    assert payload["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_login_cookie_str_requires_non_empty_string():
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_login(ctx, {"action": "cookie_str"})
    payload = _text_payload(resp)
    assert payload["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_login_from_browser_reports_import_failed_on_no_login(monkeypatch):
    """CookieImportError (e.g. user not logged in) must surface as
    import_failed, not as a generic internal error — the user should see
    actionable guidance."""
    from xhs.cookie_import import CookieImportError

    async def fake_login_from_browser(*args, **kwargs):
        raise CookieImportError(
            "No xiaohongshu.com cookies found in any chrome profile..."
        )

    monkeypatch.setattr(handlers, "login_from_browser", fake_login_from_browser)
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_login(ctx, {"action": "from_browser", "browser": "chrome"})
    payload = _text_payload(resp)
    assert payload["error"] == "import_failed"
    assert "xiaohongshu.com" in payload["message"]


@pytest.mark.asyncio
async def test_login_from_browser_returns_valid_when_pong_succeeds(monkeypatch):
    async def fake_login_from_browser(_mgr, browser, profile):
        assert browser == "chrome"
        assert profile is None
        return 16  # 16 cookies imported

    async def fake_check(_mgr):
        return True

    monkeypatch.setattr(handlers, "login_from_browser", fake_login_from_browser)
    monkeypatch.setattr(handlers, "check_cookie_valid", fake_check)
    ctx = _make_ctx(client=AsyncMock())
    resp = await handlers.handle_xhs_login(ctx, {"action": "from_browser", "browser": "chrome"})
    payload = _text_payload(resp)
    assert payload["status"] == "valid"
    assert payload["cookie_count"] == 16


# ── xhs_status ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_reports_browser_disconnected_when_not_started(monkeypatch, tmp_path):
    browser_mgr = SimpleNamespace(is_started=False, cookie_dict={})
    # Use FakeCache so get_stats() doesn't list the project's real cache/
    # directory (and get_stats()'s call to _cache_dir() doesn't mkdir one).
    ctx = handlers.HandlerContext(browser_mgr=browser_mgr, cache_module=FakeCache())

    # Don't let the handler stat a real user cookies.json.
    monkeypatch.setattr(handlers.os.path, "exists", lambda p: False)

    resp = await handlers.handle_xhs_status(ctx, {})
    payload = _text_payload(resp)
    assert payload["browser"] == "disconnected"
    assert payload["cookie"].startswith("unknown")


# ── Response helpers ──────────────────────────────────────────────────


def test_json_response_wraps_in_text_content():
    out = handlers.json_response({"a": 1})
    assert len(out) == 1
    assert out[0].type == "text"
    assert json.loads(out[0].text) == {"a": 1}


def test_error_response_structure():
    out = handlers.error_response("oops", "bad")
    payload = json.loads(out[0].text)
    assert payload == {"error": "oops", "message": "bad"}
