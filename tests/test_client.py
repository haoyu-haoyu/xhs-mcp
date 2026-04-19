"""Regression tests for XHSClient internals that aren't covered by the
handler-level mocks in test_handlers.py.

Focus on code paths inside XHSClient that take the client's OWN mock of a
lower-level method (e.g. get_note_comments) — the handler tests can only
inject at the XHSClient boundary, so these live here instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xhs.client import XHSClient


@pytest.mark.asyncio
async def test_get_note_all_comments_skips_null_comment_slots():
    """Regression: XHS occasionally returns `{"comments": [None, {...}]}`.
    The per-comment loop in get_note_all_comments must skip None entries
    rather than raising AttributeError on comment.get(...), which would
    abort the whole detail fetch and surface as internal_error at the
    MCP layer instead of degrading gracefully per-note."""
    # SimpleNamespace stand-in — get_note_all_comments only touches
    # self._sleep_interval and self.get_note_comments.
    browser_mgr = SimpleNamespace(page=None, cookie_dict={})
    client = XHSClient(browser_mgr)

    # Short-circuit the random sleep that normally gates each page.
    async def _no_sleep():
        return None

    client._sleep_interval = _no_sleep  # type: ignore[assignment]

    # Single page of top-level comments with a null slot in the middle.
    async def fake_get_comments(note_id, xsec_token, cursor):
        return {
            "has_more": False,
            "cursor": "",
            "comments": [
                None,
                {
                    "id": "c1",
                    "content": "real",
                    "sub_comments": [],
                    "sub_comment_has_more": False,
                },
            ],
        }

    client.get_note_comments = AsyncMock(side_effect=fake_get_comments)

    result = await client.get_note_all_comments(
        note_id="n1", xsec_token="tok", max_count=10
    )
    # Null slot dropped before max_count budgeting; only the real comment
    # survives and the return type stays List[Dict].
    assert len(result) == 1
    assert result[0]["id"] == "c1"
