"""Unit tests for xhs/cookie_import.py.

The function under test reaches into the OS to enumerate Chromium
profile directories and call browser_cookie3.  We mock both — the real
Keychain prompt + on-disk SQLite is out of scope for fast unit tests.
"""

from __future__ import annotations

import os
from typing import List
from unittest.mock import patch

import pytest

from xhs import cookie_import
from xhs.cookie_import import (
    CookieImportError,
    _is_logged_in_cookie_set,
    _to_playwright_cookies,
    import_cookies_from_browser,
)


class FakeCookie:
    """Stand-in for http.cookiejar.Cookie."""

    def __init__(self, name, value, *, domain=".xiaohongshu.com",
                 path="/", secure=True, http_only=False, expires=None):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires
        self._attrs = {"HttpOnly": http_only}

    def has_nonstandard_attr(self, name):
        return self._attrs.get(name, False)


def test_is_logged_in_cookie_set_recognizes_0400_prefix():
    assert _is_logged_in_cookie_set({"web_session": "0400abcdef"}) is True


def test_is_logged_in_cookie_set_rejects_guest_prefix():
    assert _is_logged_in_cookie_set({"web_session": "030037aabbcc"}) is False
    assert _is_logged_in_cookie_set({}) is False
    assert _is_logged_in_cookie_set({"web_session": ""}) is False


def test_to_playwright_cookies_drops_empty_values():
    """Decryption-failed cookies (value="") must be filtered, not propagated."""
    jar = [
        FakeCookie("web_session", "0400real"),
        FakeCookie("locked_one", ""),
        FakeCookie("a1", "value-a1", http_only=True, expires=2000000000),
    ]
    out = _to_playwright_cookies(jar)
    names = {c["name"] for c in out}
    assert names == {"web_session", "a1"}
    a1 = next(c for c in out if c["name"] == "a1")
    assert a1["httpOnly"] is True
    assert a1["expires"] == 2000000000


def test_to_playwright_cookies_handles_session_cookies():
    """Cookies with no expires (session cookies) must encode as -1."""
    out = _to_playwright_cookies([FakeCookie("ephemeral", "v", expires=None)])
    assert out[0]["expires"] == -1


def test_import_rejects_unknown_browser():
    with pytest.raises(ValueError, match="Unsupported browser"):
        import_cookies_from_browser(browser="netscape")


def test_import_raises_when_no_profiles_found(tmp_path, monkeypatch):
    monkeypatch.setitem(
        cookie_import._CHROMIUM_BROWSERS,
        "chrome",
        ("chrome", str(tmp_path / "nonexistent")),
    )
    with pytest.raises(CookieImportError, match="No chrome profiles"):
        import_cookies_from_browser(browser="chrome")


def _setup_fake_chrome(tmp_path, profiles_with_files: List[str]) -> str:
    """Create a fake Chrome user-data dir with given profile names.

    Each profile gets an empty Network/Cookies file so
    _candidate_cookie_files picks it up.
    """
    root = tmp_path / "Chrome"
    root.mkdir()
    for prof in profiles_with_files:
        d = root / prof / "Network"
        d.mkdir(parents=True)
        (d / "Cookies").write_bytes(b"")
    return str(root)


def test_import_picks_logged_in_profile_over_guest(tmp_path, monkeypatch):
    """A profile with web_session=0400... must beat one with more cookies
    but only a guest session — the whole point of this feature."""
    root = _setup_fake_chrome(tmp_path, ["Default", "Profile 1"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    def fake_load(_func_name, cookie_file):
        if "Default" in cookie_file:
            # 5 guest cookies — would win on count alone
            return [FakeCookie(f"c{i}", f"v{i}") for i in range(5)] + [
                FakeCookie("web_session", "030037guest"),
            ]
        if "Profile 1" in cookie_file:
            return [
                FakeCookie("web_session", "0400real_session"),
                FakeCookie("a1", "real_a1"),
            ]
        return []

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        out = import_cookies_from_browser(browser="chrome")
    names = {c["name"] for c in out}
    assert "web_session" in names
    ws = next(c for c in out if c["name"] == "web_session")
    assert ws["value"] == "0400real_session"


def test_import_refuses_guest_only_by_default(tmp_path, monkeypatch):
    """Guest-only profiles must not silently overwrite a previously good
    cached session.  require_logged_in=True (the default) raises."""
    root = _setup_fake_chrome(tmp_path, ["Default"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    def fake_load(_func_name, cookie_file):
        return [FakeCookie("web_session", "030037guest"), FakeCookie("a1", "x")]

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        with pytest.raises(CookieImportError, match="guest"):
            import_cookies_from_browser(browser="chrome")


def test_import_allows_guest_when_require_logged_in_false(tmp_path, monkeypatch):
    """Diagnostic / inspection callers can opt into guest data."""
    root = _setup_fake_chrome(tmp_path, ["Default"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    def fake_load(_func_name, cookie_file):
        return [FakeCookie("web_session", "030037guest"), FakeCookie("a1", "x")]

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        out = import_cookies_from_browser(browser="chrome", require_logged_in=False)
    assert any(c["name"] == "web_session" for c in out)


def test_import_raises_when_no_xhs_cookies_anywhere(tmp_path, monkeypatch):
    root = _setup_fake_chrome(tmp_path, ["Default"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", return_value=[]):
        with pytest.raises(CookieImportError, match="No xiaohongshu.com cookies"):
            import_cookies_from_browser(browser="chrome")


def test_import_skips_profile_when_browser_cookie3_raises(tmp_path, monkeypatch):
    """A locked / unsupported profile must not abort the whole scan —
    we should fall through to the next profile."""
    root = _setup_fake_chrome(tmp_path, ["Default", "Profile 1"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    call_log = []

    def fake_load(_func, cookie_file):
        call_log.append(cookie_file)
        if "Default" in cookie_file:
            raise PermissionError("DB locked")
        return [FakeCookie("web_session", "0400ok"), FakeCookie("a1", "a1v")]

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        out = import_cookies_from_browser(browser="chrome")
    assert len(call_log) == 2  # both attempted
    assert any(c["name"] == "web_session" for c in out)


def test_import_failure_message_aggregates_per_profile_errors(tmp_path, monkeypatch):
    """If every profile fails, the resulting CookieImportError must
    surface each profile's actual error so an operator can fix the root
    cause (e.g. close Chrome to release the DB lock)."""
    root = _setup_fake_chrome(tmp_path, ["Default", "Profile 1"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    def fake_load(_func, cookie_file):
        if "Default" in cookie_file:
            raise PermissionError("DB locked by Chrome")
        raise OSError("disk error")

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        with pytest.raises(CookieImportError) as exc:
            import_cookies_from_browser(browser="chrome")
    msg = str(exc.value)
    assert "PermissionError" in msg
    assert "DB locked" in msg
    assert "disk error" in msg


def test_import_with_explicit_profile_only_tries_that_one(tmp_path, monkeypatch):
    root = _setup_fake_chrome(tmp_path, ["Default", "Profile 1"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))

    call_log = []

    def fake_load(_func, cookie_file):
        call_log.append(cookie_file)
        return [FakeCookie("web_session", "0400")]

    with patch.object(cookie_import, "_load_xhs_cookies_for_profile", side_effect=fake_load):
        import_cookies_from_browser(browser="chrome", profile="Profile 1")
    assert len(call_log) == 1
    assert "Profile 1" in call_log[0]


def test_import_explicit_profile_not_found_raises(tmp_path, monkeypatch):
    root = _setup_fake_chrome(tmp_path, ["Default"])
    monkeypatch.setitem(cookie_import._CHROMIUM_BROWSERS, "chrome", ("chrome", root))
    with pytest.raises(CookieImportError, match="Profile 'Profile 9' not found"):
        import_cookies_from_browser(browser="chrome", profile="Profile 9")


def test_import_prefers_network_cookies_over_legacy_path(tmp_path, monkeypatch):
    """Newer Chrome stores cookies under <profile>/Network/Cookies.  When
    both files exist, the newer one must win."""
    root = tmp_path / "Chrome"
    (root / "Default" / "Network").mkdir(parents=True)
    (root / "Default" / "Network" / "Cookies").write_bytes(b"")
    (root / "Default" / "Cookies").write_bytes(b"")  # legacy
    monkeypatch.setitem(
        cookie_import._CHROMIUM_BROWSERS,
        "chrome",
        ("chrome", str(root)),
    )

    candidates = cookie_import._candidate_cookie_files(str(root))
    assert len(candidates) == 1
    assert candidates[0][0] == "Default"
    assert candidates[0][1].endswith(os.path.join("Network", "Cookies"))
