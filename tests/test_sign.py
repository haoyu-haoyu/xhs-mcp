"""Unit tests for xhs/sign.py.

The signing implementation is now a thin wrapper over xhshow's
``Xhshow.sign_headers``.  These tests pin the public contract of
``sign_request`` (return-key shape, GET/POST dispatch, method
normalization) without depending on the network or a browser.
"""

from __future__ import annotations

import pytest

from xhs import sign


_REQUIRED_KEYS = {"x-s", "x-s-common", "x-t", "x-b3-traceid", "x-xray-traceid"}
_FAKE_COOKIES = {"a1": "fake_a1", "web_session": "fake_session", "webId": "fake_web_id"}


def test_sign_request_get_returns_full_header_set():
    headers = sign.sign_request(
        uri="/api/sns/web/v1/user/selfinfo",
        cookies=_FAKE_COOKIES,
        data={},
        method="GET",
    )
    assert _REQUIRED_KEYS.issubset(headers.keys())
    assert headers["x-s"].startswith("XYS_")


def test_sign_request_post_returns_full_header_set():
    headers = sign.sign_request(
        uri="/api/sns/web/v1/search/notes",
        cookies=_FAKE_COOKIES,
        data={"keyword": "test", "page": 1},
        method="POST",
    )
    assert _REQUIRED_KEYS.issubset(headers.keys())


def test_sign_request_method_is_case_insensitive():
    lower = sign.sign_request(uri="/x", cookies=_FAKE_COOKIES, data={}, method="get")
    upper = sign.sign_request(uri="/x", cookies=_FAKE_COOKIES, data={}, method="GET")
    assert lower.keys() == upper.keys()


def test_sign_request_rejects_unknown_method():
    with pytest.raises(ValueError):
        sign.sign_request(uri="/x", cookies=_FAKE_COOKIES, method="DELETE")


def test_sign_request_accepts_cookie_string():
    cookie_str = "a1=fake_a1;web_session=fake_session"
    headers = sign.sign_request(
        uri="/api/sns/web/v1/user/selfinfo",
        cookies=cookie_str,
        data={},
        method="GET",
    )
    assert _REQUIRED_KEYS.issubset(headers.keys())


def test_sign_request_accepts_none_data():
    """data=None must be valid (parameter-less GET / empty-body POST)."""
    headers = sign.sign_request(
        uri="/api/sns/web/v1/user/selfinfo",
        cookies=_FAKE_COOKIES,
        data=None,
        method="GET",
    )
    assert _REQUIRED_KEYS.issubset(headers.keys())


def test_sign_request_rejects_non_dict_data():
    """Strings/lists must raise — silent fallback would emit wrong signatures."""
    for bad in ("raw=body", ["not", "dict"], 42):
        with pytest.raises(TypeError):
            sign.sign_request(
                uri="/x",
                cookies=_FAKE_COOKIES,
                data=bad,
                method="POST",
            )
