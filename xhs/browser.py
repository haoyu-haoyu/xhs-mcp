# xhs-mcp browser management
# Author: Wang
# Extracted and simplified from MediaCrawler core.py
# License: Non-Commercial Learning Use Only

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, Page, async_playwright, Playwright

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

        # Inject stealth script to avoid headless detection
        stealth_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "libs", "stealth.min.js")
        if os.path.exists(stealth_path):
            await self._browser_context.add_init_script(path=stealth_path)
            logger.info("Stealth script injected.")

        self._page = await self._browser_context.new_page()

        # Try to load cached cookies before navigating
        await self._load_cookies_from_cache()

        # Navigate to XHS so window.mnsv2 becomes available
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

    async def save_cookies_to_cache(self) -> None:
        """Save current browser cookies to JSON cache file."""
        cookies = await self.browser_context.cookies()
        cache_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), COOKIE_CACHE_PATH)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"Cookies saved to {cache_path}")

    async def _load_cookies_from_cache(self) -> bool:
        """Load cookies from cache file into browser context."""
        cache_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), COOKIE_CACHE_PATH)
        if not os.path.exists(cache_path):
            return False
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            await self.browser_context.add_cookies(cookies)
            logger.info(f"Loaded {len(cookies)} cookies from cache.")
            return True
        except Exception as e:
            logger.warning(f"Failed to load cookies from cache: {e}")
            return False

    async def reload_page(self) -> None:
        """Reload the XHS page to refresh window.mnsv2 (e.g. after signature failure)."""
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
