# xhs-mcp browser cookie import
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# Pull a live, logged-in xiaohongshu.com session from a real installed
# browser instead of going through a Playwright QR scan.  The QR-scan
# path produces an `web_session` that XHS frequently downgrades to a
# guest session because the Playwright fingerprint differs from a real
# user agent.  Cookies that came from the user's real browser carry
# the matching fingerprint and are accepted as a true logged-in
# session.

import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("xhs-mcp")

XHS_COOKIE_DOMAIN = "xiaohongshu.com"


def _user_data_dirs() -> Dict[str, Tuple[str, str]]:
    """Per-platform Chromium-family user-data roots.

    Returns a dict mapping public browser name → (browser_cookie3
    function attr name, absolute path to the user-data root containing
    profile dirs).  Built lazily so the module imports cleanly on
    platforms where ``os.path.expanduser`` would yield odd results.
    """
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        appsup = os.path.join(home, "Library", "Application Support")
        return {
            "chrome": ("chrome", os.path.join(appsup, "Google", "Chrome")),
            "edge": ("edge", os.path.join(appsup, "Microsoft Edge")),
            "brave": ("brave", os.path.join(appsup, "BraveSoftware", "Brave-Browser")),
            "chromium": ("chromium", os.path.join(appsup, "Chromium")),
        }
    if sys.platform.startswith("linux"):
        cfg = os.path.join(home, ".config")
        return {
            "chrome": ("chrome", os.path.join(cfg, "google-chrome")),
            "edge": ("edge", os.path.join(cfg, "microsoft-edge")),
            "brave": ("brave", os.path.join(cfg, "BraveSoftware", "Brave-Browser")),
            "chromium": ("chromium", os.path.join(cfg, "chromium")),
        }
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return {
            "chrome": ("chrome", os.path.join(local, "Google", "Chrome", "User Data")),
            "edge": ("edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
            "brave": ("brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
            "chromium": ("chromium", os.path.join(local, "Chromium", "User Data")),
        }
    # Unknown platform — return empty so import() raises a clear error.
    return {}


# Mutable so tests can monkeypatch entries.  Populated at import time
# from the per-platform helper above.
_CHROMIUM_BROWSERS: Dict[str, Tuple[str, str]] = _user_data_dirs()


class CookieImportError(RuntimeError):
    """Raised when no usable xhs cookies can be found in the target browser."""


def _candidate_cookie_files(user_data_dir: str) -> List[Tuple[str, str]]:
    """Enumerate (profile_name, cookie_file_path) pairs for one Chromium install.

    Newer Chrome stores cookies under ``<profile>/Network/Cookies``; older
    versions used ``<profile>/Cookies``.  Both are checked, newer first.
    Profiles include "Default" plus any "Profile N" directories.
    """
    if not os.path.isdir(user_data_dir):
        return []

    profiles: List[str] = []
    if os.path.isdir(os.path.join(user_data_dir, "Default")):
        profiles.append("Default")
    try:
        for entry in sorted(os.listdir(user_data_dir)):
            if entry.startswith("Profile ") and os.path.isdir(os.path.join(user_data_dir, entry)):
                profiles.append(entry)
    except OSError as e:
        logger.warning(f"Could not list profiles under {user_data_dir}: {e}")
        return []

    found: List[Tuple[str, str]] = []
    for profile in profiles:
        for sub in ("Network/Cookies", "Cookies"):
            path = os.path.join(user_data_dir, profile, sub)
            if os.path.isfile(path):
                found.append((profile, path))
                break  # prefer Network/Cookies over Cookies for one profile
    return found


def _is_logged_in_cookie_set(cookie_dict: Dict[str, str]) -> bool:
    """Heuristic: a real logged-in xhs web session has a `web_session` value
    starting with ``0400`` (current scheme).  Guest sessions use shorter
    prefixes like ``030037`` and are rejected by login-required endpoints.
    """
    ws = cookie_dict.get("web_session", "")
    return ws.startswith("0400")


def _load_xhs_cookies_for_profile(
    bc_func_name: str, cookie_file: str
) -> List["object"]:
    """Call browser_cookie3.<func>(cookie_file=..., domain_name=...).

    Imported lazily so that ``import_cookies_from_browser`` doesn't pay
    the ~200ms cold-start cost in code paths that never use it.
    """
    import browser_cookie3 as bc

    func = getattr(bc, bc_func_name)
    jar = func(cookie_file=cookie_file, domain_name=XHS_COOKIE_DOMAIN)
    return list(jar)


def _to_playwright_cookies(jar_cookies: List["object"]) -> List[Dict]:
    """Convert ``http.cookiejar.Cookie`` items to Playwright's cookie dicts.

    Playwright accepts ``{name, value, domain, path, secure, httpOnly,
    expires}`` — extra fields are ignored.  We propagate ``expires`` only
    when the upstream value is set (None → -1 means session cookie).
    """
    out: List[Dict] = []
    for c in jar_cookies:
        if not c.value:
            # Decryption failed silently — propagating an empty value
            # would overwrite a real one if Playwright merges with an
            # existing context.  Drop it instead.
            continue
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
                "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
                "expires": int(c.expires) if c.expires else -1,
            }
        )
    return out


def import_cookies_from_browser(
    browser: str = "chrome",
    profile: Optional[str] = None,
    require_logged_in: bool = True,
) -> List[Dict]:
    """Extract a live xhs login session from a locally installed browser.

    Args:
        browser: One of "chrome", "edge", "brave", "chromium".  Safari
            and Firefox use different storage formats and aren't supported
            here.  Safari additionally requires Full Disk Access on macOS.
        profile: Specific Chromium profile name (e.g. "Default",
            "Profile 1").  If ``None``, all profiles are scanned and the
            best-matching one (real logged-in session preferred over
            anything with more cookies) is used.
        require_logged_in: If True (default), raises CookieImportError
            unless at least one profile has a real logged-in session
            (``web_session`` prefix ``0400``).  Set to False to also
            accept guest-session cookies — primarily useful for diagnostic
            tooling, NOT for the login flow (a guest fallback would
            silently overwrite a previously-good cookie cache).

    Returns:
        Playwright-formatted cookie list ready for
        ``browser_context.add_cookies``.

    Raises:
        ValueError: ``browser`` not in the supported set.
        CookieImportError: target browser has no readable xhs cookies
            (platform unsupported, browser not installed, not logged in,
            DB locked, or Keychain decryption failed).  The message
            aggregates per-profile failure reasons so operators can
            tell apart "you're not logged in" from "your DB was locked".
    """
    if not _CHROMIUM_BROWSERS:
        raise CookieImportError(
            f"Browser cookie import is not supported on platform "
            f"'{sys.platform}'. Use action=qrcode or action=cookie_str instead."
        )
    if browser not in _CHROMIUM_BROWSERS:
        raise ValueError(
            f"Unsupported browser '{browser}'. Choose one of: "
            f"{', '.join(sorted(_CHROMIUM_BROWSERS))}"
        )

    bc_func_name, user_data_dir = _CHROMIUM_BROWSERS[browser]
    candidates = _candidate_cookie_files(user_data_dir)
    if not candidates:
        raise CookieImportError(
            f"No {browser} profiles found under {user_data_dir}. "
            "Is the browser installed?"
        )

    if profile is not None:
        candidates = [(p, f) for (p, f) in candidates if p == profile]
        if not candidates:
            raise CookieImportError(
                f"Profile '{profile}' not found for {browser}."
            )

    best_jar: Optional[List["object"]] = None
    best_profile: Optional[str] = None
    best_score = -1  # higher is better
    failures: List[str] = []  # (profile, error) for diagnostics
    for prof_name, cookie_file in candidates:
        try:
            jar = _load_xhs_cookies_for_profile(bc_func_name, cookie_file)
        except (PermissionError, OSError, ImportError) as e:
            # Distinguishable system errors — surface them in the final
            # error message so the operator can fix the root cause
            # (e.g. close Chrome to release the DB lock, grant Full
            # Disk Access, install browser-cookie3) instead of being
            # told to "log in" when they already are.
            msg = f"{prof_name}: {type(e).__name__}: {e}"
            failures.append(msg)
            logger.warning(f"[cookie_import] {browser}/{msg}")
            continue
        except Exception as e:  # noqa: BLE001 — last-resort catch-all
            # Anything else (e.g. browser_cookie3's BrowserCookieError,
            # sqlite3.DatabaseError, KeychainException).  Same handling:
            # don't abort the whole scan, but make the failure visible.
            msg = f"{prof_name}: {type(e).__name__}: {e}"
            failures.append(msg)
            logger.warning(f"[cookie_import] {browser}/{msg}")
            continue

        cookie_dict = {c.name: c.value for c in jar if c.value}
        if not cookie_dict:
            failures.append(f"{prof_name}: no xhs cookies")
            continue

        # Score: a real logged-in session beats a guest session even if
        # the guest one technically has more cookies.
        score = len(cookie_dict) + (1000 if _is_logged_in_cookie_set(cookie_dict) else 0)
        if score > best_score:
            best_jar = jar
            best_profile = prof_name
            best_score = score

    if not best_jar:
        detail = ("; ".join(failures)) if failures else "no profiles produced cookies"
        raise CookieImportError(
            f"No xiaohongshu.com cookies found in any {browser} profile "
            f"({detail}). Open xiaohongshu.com in the browser and log in "
            "first; if you are logged in, ensure the browser is closed so "
            "the cookie DB isn't locked."
        )

    cookie_dict = {c.name: c.value for c in best_jar if c.value}
    if not _is_logged_in_cookie_set(cookie_dict):
        if require_logged_in:
            raise CookieImportError(
                f"{browser}/{best_profile} only has a guest xhs session "
                "(web_session does not start with '0400'). Open "
                "xiaohongshu.com in that browser, log in, then retry. "
                "Refusing to import — guest cookies would overwrite any "
                "real cached session."
            )
        logger.warning(
            f"[cookie_import] {browser}/{best_profile} cookies look like a "
            "guest session. XHS will reject login-required endpoints."
        )
    else:
        logger.info(
            f"[cookie_import] Selected {browser}/{best_profile} — "
            f"{len(cookie_dict)} xhs cookies, logged-in session."
        )

    return _to_playwright_cookies(best_jar)
