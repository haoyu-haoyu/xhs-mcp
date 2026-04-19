"""Unit test for the cookie-file permission hardening.

Verifies the REAL ``BrowserManager.save_cookies_to_cache`` and
``_load_cookies_from_cache`` methods (not a reimplementation): subclass
the manager to redirect ``_cookie_cache_path()`` into tmp_path, then
drive the actual I/O through the production code path so the test
would fail if the production chmod/os.open sequence regresses.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from xhs import browser as browser_module


def _make_manager(target_path, cookies_payload):
    """Return a BrowserManager whose cookie cache points at ``target_path``.

    Subclasses just enough to redirect the cache-path derivation; every
    other method (including save_cookies_to_cache's os.open + os.replace
    + chmod sequence) is the real production code.
    """

    class _FakeBrowserContext:
        """Stand-in for Playwright's BrowserContext — only ``cookies`` is used."""

        async def cookies(self):
            return cookies_payload

        async def add_cookies(self, cookies):
            return None

    class PatchedManager(browser_module.BrowserManager):
        def _cookie_cache_path(self):  # type: ignore[override]
            return str(target_path)

    mgr = PatchedManager()
    mgr._browser_context = _FakeBrowserContext()
    return mgr


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
async def test_save_cookies_writes_600_via_real_method(tmp_path):
    """BrowserManager.save_cookies_to_cache must write mode 0o600.

    Calls the real production method end-to-end.  If the os.open /
    os.replace / os.chmod sequence in the production code regresses
    (e.g. back to a plain ``open(path, 'w')`` with umask-dependent mode),
    this test fails.
    """
    target = tmp_path / "cookies.json"
    payload = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com", "path": "/"}]
    mgr = _make_manager(target, payload)

    await mgr.save_cookies_to_cache()

    assert target.exists()
    mode = os.stat(target).st_mode & 0o777
    assert mode & 0o077 == 0, f"cookies file has permissive mode {oct(mode)}"
    assert mode & stat.S_IRUSR
    assert mode & stat.S_IWUSR
    # Round-trip content sanity — catches silent corruption.
    assert json.loads(target.read_text(encoding="utf-8")) == payload


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
async def test_save_cleans_up_temp_on_write_error(tmp_path, monkeypatch):
    """If the JSON serialization blows up, no ``.cookies-*.tmp`` must linger."""
    target = tmp_path / "cookies.json"
    payload = [{"name": "a1", "value": "x"}]
    mgr = _make_manager(target, payload)

    # Make json.dump raise mid-write so we exercise the cleanup branch.
    def boom(*a, **kw):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr("xhs.browser.json.dump", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        await mgr.save_cookies_to_cache()

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".cookies-")]
    assert leftovers == [], f"temp files were not cleaned up: {leftovers}"
    assert not target.exists(), "primary cookies file should not exist on failure"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
async def test_load_retroactively_fixes_permissive_mode_via_real_method(tmp_path):
    """Pre-existing cookies.json with mode 0o644 must be chmod'd to 0o600 on load.

    Drives the real _load_cookies_from_cache method — not a copy of its
    permission check — so a regression in the production branch fails
    this test.
    """
    target = tmp_path / "cookies.json"
    target.write_text("[]", encoding="utf-8")
    os.chmod(target, 0o644)
    assert os.stat(target).st_mode & 0o077 != 0

    mgr = _make_manager(target, [])

    loaded = await mgr._load_cookies_from_cache()
    assert loaded is True

    mode = os.stat(target).st_mode & 0o777
    assert mode & 0o077 == 0, (
        f"_load_cookies_from_cache did not tighten permissive mode; still {oct(mode)}"
    )
