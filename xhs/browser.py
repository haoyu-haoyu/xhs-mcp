# xhs-mcp browser management
# Author: Wang
# Extracted and simplified from MediaCrawler core.py
# License: Non-Commercial Learning Use Only

import json
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    Error as PlaywrightError,
)

from config.settings import (
    HEADLESS,
    USER_AGENT,
    XHS_INDEX_URL,
    COOKIE_CACHE_PATH,
)

logger = logging.getLogger("xhs-mcp")


class BrowserManager:
    """Manages a Playwright browser instance for XHS signature generation and login."""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[object] = None  # Browser instance
        self._browser_context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cookie_dict: Dict[str, str] = {}
        self._started = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def browser_context(self) -> BrowserContext:
        if self._browser_context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._browser_context

    @property
    def cookie_dict(self) -> Dict[str, str]:
        return self._cookie_dict

    @property
    def is_started(self) -> bool:
        return self._started

    async def start(self, headless: Optional[bool] = None) -> None:
        """Launch browser, inject stealth script, navigate to XHS.

        Args:
            headless: Override headless setting. None uses config default.
                      Set to False for QR code login (must see browser).
        """
        if self._started:
            return

        use_headless = headless if headless is not None else HEADLESS
        logger.info(f"Starting Playwright browser (headless={use_headless})...")
        self._playwright = await async_playwright().start()

        chromium = self._playwright.chromium
        self._browser = await chromium.launch(headless=use_headless)
        self._browser_context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
        )

        # Inject stealth script to avoid headless detection.  The file
        # lives inside the xhs/ package so it ships with wheels too
        # (not just editable-from-checkout installs).
        stealth_path = os.path.join(os.path.dirname(__file__), "stealth.min.js")
        if os.path.exists(stealth_path):
            await self._browser_context.add_init_script(path=stealth_path)
            logger.info("Stealth script injected.")
        else:
            logger.warning(
                f"Stealth script not found at {stealth_path}; bot detection "
                "mitigation is disabled.  Reinstall the package to restore it."
            )

        self._page = await self._browser_context.new_page()

        # Try to load cached cookies before navigating
        await self._load_cookies_from_cache()

        # Navigate to XHS so the page can establish a real session
        # (e.g. set webId / acw_tc cookies) used by the xhshow signer.
        await self._page.goto(XHS_INDEX_URL)
        logger.info(f"Navigated to {XHS_INDEX_URL}")

        # Refresh cookie dict from browser
        await self.refresh_cookies()
        self._started = True
        logger.info("Browser started successfully.")

    async def stop(self) -> None:
        """Close browser and playwright."""
        if self._browser_context:
            await self._browser_context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._started = False
        self._page = None
        self._browser_context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser stopped.")

    async def restart(self, headless: Optional[bool] = None) -> None:
        """Stop and restart the browser (e.g. to switch headless mode)."""
        await self.stop()
        await self.start(headless=headless)

    async def refresh_cookies(self) -> Tuple[str, Dict[str, str]]:
        """Read current cookies from browser context and update internal state."""
        cookies = await self.browser_context.cookies()
        cookie_str, cookie_dict = _convert_cookies(cookies)
        self._cookie_dict = cookie_dict
        return cookie_str, cookie_dict

    async def get_cookie_str(self) -> str:
        """Get current cookies as a semicolon-separated string."""
        cookie_str, _ = await self.refresh_cookies()
        return cookie_str

    def _cookie_cache_path(self) -> str:
        """Return the absolute path of the cookies cache file.

        Split into its own method so tests can subclass the manager with
        a tmp_path target instead of monkey-patching module globals.
        """
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), COOKIE_CACHE_PATH)

    async def save_cookies_to_cache(self) -> None:
        """Save current browser cookies to JSON cache file.

        Cookies are sensitive — they grant full account access.  The file
        is written with mode 0o600 (user-only read/write) on POSIX systems
        so other local users on shared machines cannot read them.  On
        Windows ``os.chmod`` only affects the read-only bit and the other
        Unix mode bits are ignored; real access control there comes from
        NTFS ACLs (see README for user-facing mitigation).

        Concurrency: uses ``tempfile.mkstemp`` in the destination directory
        so two writers never collide on the same temp filename, then
        ``os.replace`` promotes atomically.  On a crash mid-write the
        partial temp file is cleaned up; the previous cookie file is
        untouched.
        """
        cookies = await self.browser_context.cookies()
        cache_path = self._cookie_cache_path()
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, exist_ok=True)

        # Unique temp file in the same directory so the rename is atomic
        # on POSIX (same filesystem) and each concurrent writer gets its
        # own temp path — a fixed ".tmp" suffix was racy.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".cookies-",
            suffix=".tmp",
            dir=cache_dir,
        )
        # Track whether the temp file still needs cleanup.  Set to False
        # once os.replace() succeeds (after which the original tmp path
        # no longer exists as a distinct file).
        needs_cleanup = True
        try:
            # mkstemp opens with 0o600 on POSIX by default; still be
            # explicit here for defensive programming.
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass  # Best-effort on platforms without POSIX perms.
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, cache_path)
            needs_cleanup = False  # tmp_path has been consumed by replace
        finally:
            # Clean up the partial / leftover temp file on any failure —
            # whether json.dump raised during write or os.replace raised
            # during the rename (e.g. destination locked on Windows).
            # Prevents stale ".cookies-*.tmp" files containing live
            # cookies from accumulating in config/.
            if needs_cleanup:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        # Re-assert mode after replace — on some filesystems the rename
        # may inherit the destination's pre-existing mode bits.
        try:
            os.chmod(cache_path, 0o600)
        except OSError as e:
            logger.warning(f"Could not chmod {cache_path} to 0600: {e}")

        logger.info(f"Cookies saved to {cache_path} (mode 0600)")

    async def _load_cookies_from_cache(self) -> bool:
        """Load cookies from cache file into browser context."""
        cache_path = self._cookie_cache_path()
        if not os.path.exists(cache_path):
            return False

        # Retroactively harden permissions if the file was written by an
        # older version that didn't set mode 0o600.  Cheap; skip on
        # Windows where os.stat().st_mode has different semantics.
        if os.name == "posix":
            try:
                current_mode = os.stat(cache_path).st_mode & 0o777
                if current_mode & 0o077:
                    logger.warning(
                        f"Cookie cache {cache_path} has permissive mode "
                        f"{oct(current_mode)}; tightening to 0600."
                    )
                    os.chmod(cache_path, 0o600)
            except OSError as e:
                logger.warning(f"Could not check/fix cookie cache perms: {e}")

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            # Playwright's add_cookies expects a list of dicts.  If the
            # cached payload is malformed (e.g. someone wrote "{}" or a
            # stray string into the file) we don't want startup to crash
            # — treat it as "no usable cache" so the first request falls
            # back to the login flow.
            await self.browser_context.add_cookies(cookies)
            logger.info(f"Loaded {len(cookies)} cookies from cache.")
            return True
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning(
                f"Failed to load cookies from cache ({type(e).__name__}: {e}); "
                "treating as empty.  Delete the file or re-login to reset."
            )
            return False
        except PlaywrightError as e:
            # Playwright rejects structurally-bad cookie objects
            # (missing name/value, invalid domain, etc.).  Same graceful
            # handling as above — operators can inspect the log and
            # decide whether to wipe the file.
            logger.warning(
                f"Playwright rejected cached cookies ({type(e).__name__}: {e}); "
                "cache is unusable.  Re-login to replace it."
            )
            return False

    async def reload_page(self) -> None:
        """Reload the XHS page to refresh the browser session cookies."""
        if self._page:
            await self._page.reload()
            logger.info("Page reloaded.")


def _convert_cookies(cookies: Optional[List[dict]]) -> Tuple[str, Dict[str, str]]:
    """Convert Playwright cookies list to string and dict."""
    if not cookies:
        return "", {}
    cookie_str = ";".join(f"{c.get('name')}={c.get('value')}" for c in cookies)
    cookie_dict = {c.get("name"): c.get("value") for c in cookies}
    return cookie_str, cookie_dict


def convert_str_cookie_to_dict(cookie_str: str) -> Dict[str, str]:
    """Parse a cookie header string into a dict."""
    cookie_dict: Dict[str, str] = {}
    if not cookie_str:
        return cookie_dict
    for cookie in cookie_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        parts = cookie.split("=", 1)
        if len(parts) == 2:
            cookie_dict[parts[0]] = parts[1]
    return cookie_dict
