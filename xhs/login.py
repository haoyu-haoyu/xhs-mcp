# xhs-mcp login management
# Author: Wang
# Extracted and simplified from MediaCrawler login.py
# License: Non-Commercial Learning Use Only
#
# Supports: QR code login + Cookie string import
# Removed: phone/SMS login, CacheFactory dependency

import asyncio
import logging

from playwright.async_api import (
    BrowserContext,
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from .browser import BrowserManager, convert_str_cookie_to_dict

logger = logging.getLogger("xhs-mcp")

# Selector-probing helpers below catch Playwright's exceptions rather than
# bare `Exception`.  A TimeoutError is expected (the selector isn't on
# this page yet); other PlaywrightErrors usually mean the element exists
# but isn't actionable (detached, hidden, navigated away).  In both
# cases the intended behaviour is "try the next selector", but we log
# at DEBUG so troubleshooters can see what got rejected and why.
_EXPECTED_PROBE_ERRORS = (PlaywrightTimeoutError, PlaywrightError)

# Multiple selectors to try, in order of preference.
# XHS updates their frontend frequently so we need fallbacks.
_LOGIN_BUTTON_SELECTORS = [
    'button[class*="login"]',           # class contains "login"
    'span:text("登录")',                 # text match
    'div.login-btn',                     # common class
    "xpath=//*[@id='app']//button",      # generic first button in app header
]

_QRCODE_IMG_SELECTORS = [
    'img.qrcode-img',                    # original class
    'img[class*="qrcode"]',              # class contains "qrcode"
    'div[class*="qrcode"] img',          # img inside qrcode container
    'div.login-container img',           # img inside login container
    'div[class*="login"] img[src*="qrcode"]',
]


async def _find_and_click(page: Page, selectors: list[str], timeout: int = 5000) -> bool:
    """Try multiple selectors, click the first one found."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=timeout)
                logger.info(f"Clicked element: {sel}")
                return True
        except _EXPECTED_PROBE_ERRORS as e:
            logger.debug(f"Selector '{sel}' not clickable: {type(e).__name__}: {e}")
            continue
    return False


async def _wait_for_any(page: Page, selectors: list[str], timeout: int = 10000) -> bool:
    """Wait for any of the selectors to appear."""
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=timeout)
            logger.info(f"Found element: {sel}")
            return True
        except _EXPECTED_PROBE_ERRORS as e:
            logger.debug(f"Selector '{sel}' did not appear: {type(e).__name__}: {e}")
            continue
    return False


async def check_login_state(
    context_page: Page,
    browser_context: BrowserContext,
    no_logged_in_session: str,
    timeout_seconds: int = 120,
) -> bool:
    """Poll login state until success or timeout.

    Checks both UI elements and cookie changes.
    Returns True if login confirmed, False if timed out.
    """
    from .browser import _convert_cookies

    for elapsed in range(timeout_seconds):
        if elapsed % 15 == 0 and elapsed > 0:
            logger.info(f"Still waiting for login... ({elapsed}s / {timeout_seconds}s)")

        # Check 1: UI element - profile link in sidebar.  Probe is allowed
        # to miss silently (timeout/detached element) while polling.
        try:
            profile_selectors = [
                "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']",
                "a[href*='/user/profile/'] span",
                "a[href*='/user/profile/']",
            ]
            for sel in profile_selectors:
                if await context_page.is_visible(sel, timeout=500):
                    logger.info("Login confirmed via UI element.")
                    return True
        except _EXPECTED_PROBE_ERRORS as e:
            logger.debug(f"Profile-selector probe failed: {type(e).__name__}: {e}")

        # Check 2: Cookie-based detection - web_session changed.  Only
        # treat Playwright errors as "try again next tick"; any other
        # error (e.g. the context was closed out from under us) should
        # propagate so the caller notices.
        try:
            current_cookies = await browser_context.cookies()
            _, cookie_dict = _convert_cookies(current_cookies)
            current_session = cookie_dict.get("web_session")
            if current_session and current_session != no_logged_in_session:
                logger.info("Login confirmed via cookie change.")
                return True
        except PlaywrightError as e:
            logger.debug(f"Cookie read transient failure: {type(e).__name__}: {e}")

        await asyncio.sleep(1)

    return False


async def login_by_qrcode(browser_mgr: BrowserManager) -> bool:
    """Open visible browser, show QR code, wait for user to scan.

    The browser MUST be started with headless=False before calling this.
    Returns True if login successful, False if timed out.
    """
    page = browser_mgr.page
    context = browser_mgr.browser_context

    logger.info("Starting QR code login flow...")

    # Dismiss any cookie consent popup first.  We iterate best-effort;
    # any Playwright error (no match, element detached, iframe nav)
    # means "nothing to dismiss" — not a real failure.
    try:
        for btn_text in ["Accept all cookies", "接受所有", "全部接受", "同意"]:
            loc = page.locator(f'button:text("{btn_text}")')
            if await loc.count() > 0:
                await loc.first.click()
                logger.info(f"Dismissed popup: {btn_text}")
                await asyncio.sleep(1)
                break
    except _EXPECTED_PROBE_ERRORS as e:
        logger.debug(f"Cookie popup dismiss skipped: {type(e).__name__}: {e}")

    # Step 1: Try to find QR code directly (maybe login dialog auto-popped)
    qrcode_found = await _wait_for_any(page, _QRCODE_IMG_SELECTORS, timeout=3000)

    # Step 2: If not found, click login button to trigger the dialog
    if not qrcode_found:
        logger.info("QR code not visible, clicking login button...")
        clicked = await _find_and_click(page, _LOGIN_BUTTON_SELECTORS, timeout=5000)
        if clicked:
            await asyncio.sleep(2)
            qrcode_found = await _wait_for_any(page, _QRCODE_IMG_SELECTORS, timeout=10000)

    # Step 3: If still not found, navigate to login page directly.
    # Navigation may fail on flaky network or if the page redirects to
    # geo-blocked content — log the concrete reason so the user can
    # diagnose, then continue to the manual-browser fallback below.
    if not qrcode_found:
        logger.info("Trying direct navigation to login page...")
        try:
            await page.goto("https://www.xiaohongshu.com/login")
            await asyncio.sleep(3)
            qrcode_found = await _wait_for_any(page, _QRCODE_IMG_SELECTORS, timeout=10000)
        except _EXPECTED_PROBE_ERRORS as e:
            logger.warning(
                f"Direct login-page navigation failed ({type(e).__name__}: {e}); "
                "falling back to manual browser interaction."
            )

    if not qrcode_found:
        logger.warning(
            "Could not find QR code automatically. "
            "The browser window is open — please manually navigate to login and scan the QR code. "
            "Waiting up to 120 seconds for login to complete..."
        )
        # Even if we can't find the QR code element, we still wait —
        # the user can manually interact with the visible browser

    # Record pre-login session for change detection
    from .browser import _convert_cookies
    current_cookies = await context.cookies()
    _, cookie_dict = _convert_cookies(current_cookies)
    no_logged_in_session = cookie_dict.get("web_session", "")

    if qrcode_found:
        logger.info("QR code displayed. Please scan with Xiaohongshu app. Waiting up to 120 seconds...")

    success = await check_login_state(page, context, no_logged_in_session, timeout_seconds=120)

    if success:
        logger.info("Login successful! Saving cookies...")
        await asyncio.sleep(3)  # Wait for redirect
        await browser_mgr.refresh_cookies()
        await browser_mgr.save_cookies_to_cache()
        return True
    else:
        logger.error("Login timed out after 120 seconds.")
        return False


async def login_by_cookie_str(browser_mgr: BrowserManager, cookie_str: str) -> bool:
    """Import cookies from a string (e.g. from browser dev tools).

    Sets all cookies on .xiaohongshu.com domain.
    Returns True if cookies were set.
    """
    cookie_dict = convert_str_cookie_to_dict(cookie_str)
    context = browser_mgr.browser_context

    cookies_to_set = []
    for key, value in cookie_dict.items():
        cookies_to_set.append({
            "name": key,
            "value": value,
            "domain": ".xiaohongshu.com",
            "path": "/",
        })

    if cookies_to_set:
        await context.add_cookies(cookies_to_set)
        await browser_mgr.reload_page()
        await browser_mgr.refresh_cookies()
        await browser_mgr.save_cookies_to_cache()
        logger.info(f"Set {len(cookies_to_set)} cookies from string.")
        return True

    return False


async def check_cookie_valid(browser_mgr: BrowserManager) -> bool:
    """Check if current cookies are valid by querying self info API."""
    from .client import XHSClient
    client = XHSClient(browser_mgr)
    return await client.pong()
