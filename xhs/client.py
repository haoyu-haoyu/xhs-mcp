# xhs-mcp API client
# Author: Wang
# Extracted and refactored from MediaCrawler client.py
# License: Non-Commercial Learning Use Only
#
# Standalone XHS API client — no abstract base classes, no proxy pool,
# no storage layer. Returns raw JSON dicts for MCP tools to format.

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Union

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type

from config.settings import (
    XHS_API_HOST,
    XHS_INDEX_URL,
    REQUEST_TIMEOUT,
    DEFAULT_HEADERS,
    CRAWL_INTERVAL_MIN,
    CRAWL_INTERVAL_MAX,
)
from .browser import BrowserManager
from .models import DataFetchError, IPBlockError, NoteNotFoundError
from .sign import sign_request

logger = logging.getLogger("xhs-mcp")


def _get_search_id() -> str:
    """Generate a search_id for XHS search API."""
    e = int(time.time() * 1000) << 64
    t = int(random.uniform(0, 2147483646))
    return _base36encode(e + t)


def _base36encode(number: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if number < 0:
        raise ValueError("Negative numbers not supported")
    if number < len(alphabet):
        return alphabet[number]
    result = ""
    while number != 0:
        number, i = divmod(number, len(alphabet))
        result = alphabet[i] + result
    return result


class XHSClient:
    """Xiaohongshu API client with Playwright-based request signing."""

    def __init__(self, browser_mgr: BrowserManager):
        self._browser = browser_mgr
        self._host = XHS_API_HOST
        self._domain = XHS_INDEX_URL
        self._timeout = REQUEST_TIMEOUT

        # Error codes
        self._IP_ERROR_CODE = 300012
        self._NOTE_NOT_FOUND_CODE = -510000
        self._NOTE_ABNORMAL_CODE = -510001

    def _get_headers(self) -> Dict[str, str]:
        """Build base headers with current cookies."""
        headers = DEFAULT_HEADERS.copy()
        headers["user-agent"] = self._browser._cookie_dict.get("user-agent", DEFAULT_HEADERS.get("user-agent", ""))
        # Build cookie string from browser cookie dict
        cookie_str = ";".join(f"{k}={v}" for k, v in self._browser.cookie_dict.items())
        headers["Cookie"] = cookie_str
        # Restore user-agent from settings (not from cookie)
        from config.settings import USER_AGENT
        headers["user-agent"] = USER_AGENT
        return headers

    async def _pre_headers(self, uri: str, params: Optional[Dict] = None, payload: Optional[Dict] = None) -> Dict:
        """Sign request headers via xhshow (pure-Python, no browser hook)."""
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")

        signs = sign_request(
            uri=uri,
            cookies=self._browser.cookie_dict,
            data=data,
            method=method,
        )

        headers = self._get_headers()
        headers.update({
            "X-S": signs["x-s"],
            "X-T": str(signs["x-t"]),
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
            "X-Xray-TraceId": signs["x-xray-traceid"],
        })
        return headers

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), retry=retry_if_not_exception_type(NoteNotFoundError))
    async def _request(self, method: str, url: str, **kwargs) -> Union[str, Any]:
        """Send HTTP request with retry logic."""
        return_response = kwargs.pop("return_response", False)

        async with httpx.AsyncClient() as client:
            response = await client.request(method, url, timeout=self._timeout, **kwargs)

        if response.status_code in (471, 461):
            verify_type = response.headers.get("Verifytype", "unknown")
            verify_uuid = response.headers.get("Verifyuuid", "unknown")
            raise DataFetchError(
                f"CAPTCHA triggered (type={verify_type}, uuid={verify_uuid})",
                request=response.request,
            )

        if return_response:
            return response.text

        data: Dict = response.json()
        if data.get("success"):
            return data.get("data", data.get("success", {}))
        elif data.get("code") == self._IP_ERROR_CODE:
            raise IPBlockError("IP blocked by XHS", request=response.request)
        elif data.get("code") in (self._NOTE_NOT_FOUND_CODE, self._NOTE_ABNORMAL_CODE):
            raise NoteNotFoundError(
                f"Note not found or abnormal (code={data.get('code')})",
                request=response.request,
            )
        else:
            raise DataFetchError(
                data.get("msg", response.text),
                request=response.request,
            )

    async def _get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """Signed GET request."""
        headers = await self._pre_headers(uri, params=params)
        return await self._request("GET", f"{self._host}{uri}", headers=headers, params=params)

    async def _post(self, uri: str, data: dict) -> Dict:
        """Signed POST request."""
        headers = await self._pre_headers(uri, payload=data)
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return await self._request("POST", f"{self._host}{uri}", data=json_str, headers=headers)

    async def _sleep_interval(self) -> None:
        """Random sleep between requests to avoid rate limiting."""
        await asyncio.sleep(random.uniform(CRAWL_INTERVAL_MIN, CRAWL_INTERVAL_MAX))

    # ── Public API methods ──

    async def pong(self) -> bool:
        """Check if current login session is valid.

        Returns ``False`` on any reachable-but-invalid response (HTTP
        non-200, missing success flag, transport failure, bad JSON).
        Unexpected exceptions propagate so callers notice genuinely
        broken state (e.g. browser context closed, signing broken) —
        those would otherwise masquerade as "session expired" and hide
        real bugs.
        """
        try:
            uri = "/api/sns/web/v1/user/selfinfo"
            headers = await self._pre_headers(uri, params={})
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._host}{uri}",
                    headers=headers,
                    timeout=self._timeout,
                )
            if response.status_code == 200:
                result = response.json()
                # Defensive against payloads where nested fields are None
                # OR the wrong type — e.g. {"data": null}, {"data": []}, or
                # {"data": "some string"} would otherwise raise
                # AttributeError on the chained .get() and surface as
                # internal_error in xhs_login(check) instead of "expired".
                # `or {}` alone handles None/[] (falsy), but a truthy
                # non-dict (e.g. a populated list) still has no .get().
                if not isinstance(result, dict):
                    return False
                data = result.get("data")
                if not isinstance(data, dict):
                    return False
                inner = data.get("result")
                if not isinstance(inner, dict):
                    return False
                return bool(inner.get("success"))
        except (httpx.HTTPError, ValueError) as e:
            # httpx.HTTPError covers connect/read/timeout; ValueError covers
            # response.json() on malformed payloads.
            logger.warning(f"Login check failed: {type(e).__name__}: {e}")
        return False

    async def search_notes(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        sort: str = "general",
        note_type: int = 0,
    ) -> Dict:
        """Search notes by keyword.

        Returns raw API response containing items with xsec_token.
        """
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": _get_search_id(),
            "sort": sort,
            "note_type": note_type,
            # xhs silently returns an empty result set if these two
            # fields are absent — the request is signed and accepted
            # (code:0, success:true) but `data.items` is omitted.
            # Confirmed against the live web bundle 2026-04: scroll-load
            # /search/notes always carries them.
            "ext_flags": [],
            "image_formats": ["jpg", "webp", "avif"],
        }
        return await self._post(uri, data)

    async def get_note_by_id(
        self,
        note_id: str,
        xsec_source: str = "pc_search",
        xsec_token: str = "",
    ) -> Dict:
        """Get note detail by ID.

        Args:
            note_id: Note ID
            xsec_source: Channel source (from search results)
            xsec_token: Security token (from search results)

        Returns:
            Note detail dict (note_card from API response)
        """
        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token,
        }
        uri = "/api/sns/web/v1/feed"
        res = await self._post(uri, data)
        if res and res.get("items"):
            note_card = res["items"][0]["note_card"]
            # Attach xsec info for downstream use (e.g. fetching comments)
            note_card["xsec_token"] = xsec_token
            note_card["xsec_source"] = xsec_source
            return note_card
        logger.warning(f"get_note_by_id: empty response for {note_id}")
        return {}

    async def get_note_comments(
        self,
        note_id: str,
        xsec_token: str = "",
        cursor: str = "",
    ) -> Dict:
        """Get first-level comments for a note (single page)."""
        uri = "/api/sns/web/v2/comment/page"
        params = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
        }
        return await self._get(uri, params)

    async def get_note_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str = "",
        num: int = 10,
        cursor: str = "",
    ) -> Dict:
        """Get sub-comments under a specific parent comment."""
        uri = "/api/sns/web/v2/comment/sub/page"
        params = {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": str(num),
            "cursor": cursor,
            "image_formats": "jpg,webp,avif",
            "top_comment_id": "",
            "xsec_token": xsec_token,
        }
        return await self._get(uri, params)

    async def get_note_all_comments(
        self,
        note_id: str,
        xsec_token: str = "",
        max_count: int = 20,
    ) -> List[Dict]:
        """Fetch up to max_count comments (with sub-comments) for a note."""
        result = []
        has_more = True
        cursor = ""

        while has_more and len(result) < max_count:
            comments_res = await self.get_note_comments(note_id, xsec_token, cursor)
            has_more = comments_res.get("has_more", False)
            cursor = comments_res.get("cursor", "")

            comments = comments_res.get("comments")
            if not comments:
                break

            # Drop null / non-dict slots BEFORE trimming and extending.
            # XHS occasionally returns `{"comments": [None, ...]}`; keeping
            # None entries would (a) consume a max_count slot, (b) leak
            # non-dicts into the List[Dict] return type, and (c) make
            # `comment.get(...)` raise AttributeError further down,
            # aborting the whole detail fetch instead of degrading
            # per-note.
            comments = [c for c in comments if isinstance(c, dict)]
            if not comments:
                break

            # Trim to max_count
            remaining = max_count - len(result)
            comments = comments[:remaining]

            # Fetch sub-comments for comments that have them
            for comment in comments:
                sub_comments = comment.get("sub_comments", [])
                sub_has_more = comment.get("sub_comment_has_more", False)
                sub_cursor = comment.get("sub_comment_cursor", "")
                root_id = comment.get("id", "")

                while sub_has_more and root_id:
                    await self._sleep_interval()
                    try:
                        sub_res = await self.get_note_sub_comments(
                            note_id, root_id, xsec_token, cursor=sub_cursor,
                        )
                        if not sub_res or not isinstance(sub_res, dict):
                            break
                        sub_has_more = sub_res.get("has_more", False)
                        sub_cursor = sub_res.get("cursor", "")
                        new_subs = sub_res.get("comments") or []
                        if not new_subs:
                            break
                        sub_comments.extend(new_subs)
                    except (
                        httpx.HTTPError,
                        ValueError,
                        KeyError,
                        AttributeError,
                        TypeError,
                        RuntimeError,
                    ) as e:
                        # Paginated fetch failure on a single sub-comment page
                        # stops the sub-walk but the outer comment is kept with
                        # whatever pages we already have.  AttributeError /
                        # TypeError guard against unexpected shapes returned
                        # by XHS (e.g. a list instead of a dict); RuntimeError
                        # is a defensive catch-all for unexpected runtime
                        # failures from downstream layers.  We'd rather drop
                        # sub-comments than abort the whole detail fetch.
                        # Mark the parent comment as partial so the caller
                        # knows not to cache this as a final "complete" result;
                        # otherwise one transient blip would pin truncated
                        # sub-comments for the full xhs_detail TTL.
                        logger.warning(
                            f"Failed to get sub-comments for {root_id}: "
                            f"{type(e).__name__}: {e}"
                        )
                        comment["_sub_comments_partial"] = True
                        break

                comment["sub_comments"] = sub_comments

            result.extend(comments)
            await self._sleep_interval()

        return result

    async def get_creator_info(self, user_id: str) -> Optional[Dict]:
        """Get creator info by parsing user profile page HTML.

        Extracts window.__INITIAL_STATE__ from the profile page.
        Has resilient fallback if structure changes.
        """
        uri = f"/user/profile/{user_id}"
        headers = self._get_headers()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._domain}{uri}",
                    headers=headers,
                    timeout=self._timeout,
                )
                html = response.text
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch creator page: {type(e).__name__}: {e}")
            return None

        return self._extract_creator_from_html(html)

    def _extract_creator_from_html(self, html: str) -> Optional[Dict]:
        """Extract creator info from __INITIAL_STATE__ in HTML.

        Handles structure variations gracefully.
        """
        try:
            match = re.search(
                r"<script>window\.__INITIAL_STATE__=(.+?)</script>", html, re.M
            )
            if match is None:
                logger.warning("__INITIAL_STATE__ not found in creator page HTML.")
                return None

            raw = match.group(1).replace(":undefined", ":null")
            info = json.loads(raw, strict=False)

            if info is None:
                return None

            # Primary path: info.user.userPageData
            user_data = info.get("user", {})
            if isinstance(user_data, dict):
                page_data = user_data.get("userPageData")
                if page_data:
                    return page_data

                # Fallback: try other common keys
                for key in ("userInfo", "userData", "profileData"):
                    if key in user_data and user_data[key]:
                        return user_data[key]

            # Last resort: return the whole user dict
            logger.warning("Creator HTML structure may have changed. Returning raw user data.")
            return user_data if user_data else None

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse __INITIAL_STATE__ JSON: {e}")
            return None
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            # Structure-of-object surprises (missing attrs, wrong types,
            # unexpected value shapes) — log + return None so caller can
            # mark profile_error.  Anything else (e.g. a logic bug)
            # propagates to the MCP layer's last-resort catch.
            logger.error(
                f"Structural error extracting creator info: {type(e).__name__}: {e}"
            )
            return None

    async def get_notes_by_creator(
        self,
        user_id: str,
        cursor: str = "",
        page_size: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> Dict:
        """Get a page of notes published by a creator."""
        uri = "/api/sns/web/v1/user_posted"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": user_id,
            "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        }
        return await self._get(uri, params)

    async def get_creator_notes(
        self,
        user_id: str,
        max_count: int = 5,
    ) -> List[Dict]:
        """Get recent notes from a creator, up to max_count."""
        result = []
        has_more = True
        cursor = ""

        while has_more and len(result) < max_count:
            res = await self.get_notes_by_creator(user_id, cursor)
            if not res:
                break

            has_more = res.get("has_more", False)
            cursor = res.get("cursor", "")
            notes = res.get("notes", [])
            if not notes:
                break

            remaining = max_count - len(result)
            result.extend(notes[:remaining])
            await self._sleep_interval()

        return result
