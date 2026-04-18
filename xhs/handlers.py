# xhs-mcp MCP tool handlers
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# This module contains the business logic for each MCP tool.  server.py
# owns MCP registration + dispatch; handlers.py is where the work
# actually happens.  Split out so handlers can be unit-tested without
# spinning up the MCP stdio transport.

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from mcp.types import TextContent
from playwright.async_api import Error as PlaywrightError

from . import cache as request_cache
from .browser import BrowserManager
from .client import XHSClient
from .login import check_cookie_valid, login_by_cookie_str, login_by_qrcode

logger = logging.getLogger("xhs-mcp")


# ══════════════════════════════════════════════════════════════
# Handler context
# ══════════════════════════════════════════════════════════════


@dataclass
class HandlerContext:
    """Runtime dependencies passed to every handler.

    Factoring these into a context object keeps handlers pure-ish and
    lets tests swap in a fake BrowserManager + in-memory cache without
    monkey-patching module globals.
    """

    browser_mgr: BrowserManager
    last_request_time: Optional[str] = None
    # Injection points for tests.  Default to the real modules when not overridden.
    client_factory: Any = None  # () -> XHSClient (or AsyncMock in tests)
    cache_module: Any = field(default=request_cache)

    async def ensure_client(self) -> XHSClient:
        """Start the browser if needed and return a fresh XHSClient."""
        if self.client_factory is not None:
            return self.client_factory()
        if not self.browser_mgr.is_started:
            await self.browser_mgr.start()
        return XHSClient(self.browser_mgr)

    def mark_request(self) -> None:
        """Record current time for the xhs_status tool."""
        self.last_request_time = datetime.now().isoformat()


# ══════════════════════════════════════════════════════════════
# Response helpers (pure)
# ══════════════════════════════════════════════════════════════


def json_response(data: dict) -> list[TextContent]:
    """Wrap a dict as MCP TextContent JSON response."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def error_response(error_code: str, message: str) -> list[TextContent]:
    """Return a structured error response (never throws)."""
    return json_response({"error": error_code, "message": message})


# ══════════════════════════════════════════════════════════════
# Response formatters (pure)
# ══════════════════════════════════════════════════════════════


def format_search_note(item: dict) -> dict:
    """Extract structured note info from a raw search result item.

    Defensive against payloads where nested fields are ``None`` instead
    of a missing key (XHS occasionally returns ``"note_card": null``).
    Using ``x.get(k) or {}`` coerces both "missing" and "null" to an
    empty dict so downstream ``.get()`` calls don't blow up and abort
    the whole search over a single bad item.
    """
    note_card = item.get("note_card") or {}
    user_info = note_card.get("user") or {}
    interact_info = note_card.get("interact_info") or {}

    return {
        "note_id": item.get("id", ""),
        "xsec_token": item.get("xsec_token", ""),
        "xsec_source": item.get("xsec_source", ""),
        "title": note_card.get("display_title", ""),
        "desc": note_card.get("desc", ""),
        "type": note_card.get("type", "normal"),
        "time": note_card.get("time", 0),
        "user": {
            "user_id": user_info.get("user_id", user_info.get("userid", "")),
            "nickname": user_info.get("nickname", user_info.get("nick_name", "")),
            "avatar": user_info.get("avatar", ""),
        },
        "liked_count": interact_info.get("liked_count", note_card.get("liked_count", "0")),
        "collected_count": interact_info.get(
            "collected_count", note_card.get("collected_count", "0")
        ),
        "comment_count": interact_info.get("comment_count", note_card.get("comment_count", "0")),
        "share_count": interact_info.get("share_count", note_card.get("share_count", "0")),
        "tag_list": [t.get("name", "") for t in note_card.get("tag_list", [])],
        "image_list": [
            img.get("url_default", img.get("url", ""))
            for img in note_card.get("image_list", [])
        ],
        "note_url": f"https://www.xiaohongshu.com/explore/{item.get('id', '')}",
    }


def format_note_detail(note_card: dict) -> dict:
    """Format a note detail response.

    See ``format_search_note`` — same null-vs-missing guard applies here.
    """
    note_card = note_card or {}
    user_info = note_card.get("user") or {}
    interact_info = note_card.get("interact_info") or {}

    return {
        "note_id": note_card.get("note_id", ""),
        "title": note_card.get("display_title", note_card.get("title", "")),
        "desc": note_card.get("desc", ""),
        "type": note_card.get("type", "normal"),
        "time": note_card.get("time", 0),
        "last_update_time": note_card.get("last_update_time", 0),
        "ip_location": note_card.get("ip_location", ""),
        "user": {
            "user_id": user_info.get("user_id", user_info.get("userid", "")),
            "nickname": user_info.get("nickname", user_info.get("nick_name", "")),
            "avatar": user_info.get("avatar", ""),
        },
        "liked_count": interact_info.get("liked_count", note_card.get("liked_count", "0")),
        "collected_count": interact_info.get(
            "collected_count", note_card.get("collected_count", "0")
        ),
        "comment_count": interact_info.get("comment_count", note_card.get("comment_count", "0")),
        "share_count": interact_info.get("share_count", note_card.get("share_count", "0")),
        "tag_list": [t.get("name", "") for t in note_card.get("tag_list", [])],
        "image_list": [
            img.get("url_default", img.get("url", ""))
            for img in note_card.get("image_list", [])
        ],
        "note_url": f"https://www.xiaohongshu.com/explore/{note_card.get('note_id', '')}",
    }


def format_comment(comment: dict) -> dict:
    """Format a single comment.

    Guards against null nested fields the same way search/detail do.
    """
    comment = comment or {}
    user = comment.get("user_info") or {}
    sub_comments_raw = comment.get("sub_comments") or []
    sub_comments = []
    for sc in sub_comments_raw:
        sc = sc or {}
        sc_user = sc.get("user_info") or {}
        sub_comments.append({
            "comment_id": sc.get("id", ""),
            "content": sc.get("content", ""),
            "user": {
                "nickname": sc_user.get("nickname", ""),
                "user_id": sc_user.get("user_id", ""),
            },
            "ip_location": sc.get("ip_location", ""),
            "like_count": sc.get("like_count", 0),
            "create_time": sc.get("create_time", 0),
        })

    return {
        "comment_id": comment.get("id", ""),
        "content": comment.get("content", ""),
        "user": {
            "nickname": user.get("nickname", ""),
            "user_id": user.get("user_id", ""),
        },
        "ip_location": comment.get("ip_location", ""),
        "like_count": comment.get("like_count", 0),
        "create_time": comment.get("create_time", 0),
        "sub_comments": sub_comments,
    }


# ══════════════════════════════════════════════════════════════
# Tool handlers
# ══════════════════════════════════════════════════════════════


async def handle_xhs_search(ctx: HandlerContext, arguments: dict) -> list[TextContent]:
    """Handle xhs_search tool call.

    Supports multiple keywords with 5-10s gap between different keywords.
    Each API request has 2-5s random interval.
    """
    keywords: List[str] = arguments.get("keywords", [])
    sort: str = arguments.get("sort", "general")
    page: int = arguments.get("page", 1)
    note_type: int = arguments.get("note_type", 0)
    force_refresh: bool = arguments.get("force_refresh", False)

    if not keywords:
        return error_response("invalid_params", "keywords is required and cannot be empty")

    # Cache: check whole-call cache (all keywords + params combined)
    cache_params = {
        "keywords": sorted(keywords),
        "sort": sort,
        "page": page,
        "note_type": note_type,
    }
    if not force_refresh:
        cached = ctx.cache_module.get("xhs_search", cache_params)
        if cached is not None:
            cached["_from_cache"] = True
            return json_response(cached)

    try:
        client = await ctx.ensure_client()
    except (PlaywrightError, OSError, RuntimeError) as e:
        return error_response("browser_error", f"Failed to start browser: {e}")

    all_notes: List[dict] = []
    keyword_results: Dict[str, dict] = {}

    for i, keyword in enumerate(keywords):
        try:
            ctx.mark_request()
            raw = await client.search_notes(
                keyword=keyword,
                page=page,
                sort=sort,
                note_type=note_type,
            )

            # Format results, filter out non-note items (rec_query, hot_query)
            notes = []
            for item in raw.get("items", []):
                if item.get("model_type") in ("rec_query", "hot_query"):
                    continue
                notes.append(format_search_note(item))

            keyword_results[keyword] = {
                "count": len(notes),
                "has_more": raw.get("has_more", False),
            }
            all_notes.extend(notes)

        except (httpx.HTTPError, PlaywrightError, KeyError, ValueError) as e:
            logger.error(f"Search failed for keyword '{keyword}': {type(e).__name__}: {e}")
            keyword_results[keyword] = {
                "count": 0,
                "has_more": False,
                "error": f"{type(e).__name__}: {e}",
            }

        # 5-10s gap between different keywords (not after the last one)
        if i < len(keywords) - 1:
            gap = random.uniform(5, 10)
            logger.info(f"Waiting {gap:.1f}s before next keyword search...")
            await asyncio.sleep(gap)

    result = {
        "notes": all_notes,
        "total": len(all_notes),
        "keyword_results": keyword_results,
    }

    # Write to cache
    ctx.cache_module.put("xhs_search", cache_params, result)

    return json_response(result)


async def handle_xhs_detail(ctx: HandlerContext, arguments: dict) -> list[TextContent]:
    """Handle xhs_detail tool call.

    Batch fetch with per-note error handling.
    Failed notes are skipped and reported in the summary.
    Per-note caching: each note_id is cached individually (24h TTL).
    """
    note_ids: List[str] = arguments.get("note_ids", [])
    xsec_tokens: List[str] = arguments.get("xsec_tokens", [])
    get_comments: bool = arguments.get("get_comments", False)
    comment_count: int = arguments.get("comment_count", 20)
    force_refresh: bool = arguments.get("force_refresh", False)

    if not note_ids:
        return error_response("invalid_params", "note_ids is required and cannot be empty")
    if not xsec_tokens or len(xsec_tokens) != len(note_ids):
        return error_response(
            "invalid_params",
            f"xsec_tokens must be provided and match note_ids length "
            f"(got {len(note_ids)} ids, {len(xsec_tokens)} tokens)",
        )

    try:
        client = await ctx.ensure_client()
    except (PlaywrightError, OSError, RuntimeError) as e:
        return error_response("browser_error", f"Failed to start browser: {e}")

    notes: List[dict] = []
    succeeded: List[str] = []
    failed: List[dict] = []
    from_cache: List[str] = []

    for i, (note_id, xsec_token) in enumerate(zip(note_ids, xsec_tokens, strict=True)):
        note_cache_params = {
            "note_id": note_id,
            "get_comments": get_comments,
            "comment_count": comment_count,
        }

        if not force_refresh:
            cached_note = ctx.cache_module.get("xhs_detail", note_cache_params)
            if cached_note is not None:
                notes.append(cached_note)
                succeeded.append(note_id)
                from_cache.append(note_id)
                continue

        try:
            ctx.mark_request()
            note_card = await client.get_note_by_id(
                note_id=note_id,
                xsec_token=xsec_token,
            )

            if not note_card:
                failed.append({"note_id": note_id, "error": "empty_response"})
                continue

            formatted = format_note_detail(note_card)

            if get_comments:
                await asyncio.sleep(random.uniform(2, 5))
                ctx.mark_request()
                try:
                    raw_comments = await client.get_note_all_comments(
                        note_id=note_id,
                        xsec_token=xsec_token,
                        max_count=comment_count,
                    )
                    formatted["comments"] = [format_comment(c) for c in raw_comments]
                except (httpx.HTTPError, PlaywrightError, KeyError, ValueError) as ce:
                    logger.warning(
                        f"Failed to get comments for {note_id}: {type(ce).__name__}: {ce}"
                    )
                    formatted["comments"] = []
                    formatted["comments_error"] = f"{type(ce).__name__}: {ce}"

            notes.append(formatted)
            succeeded.append(note_id)
            ctx.cache_module.put("xhs_detail", note_cache_params, formatted)

        except (httpx.HTTPError, PlaywrightError, KeyError, ValueError) as e:
            logger.error(f"Failed to get detail for {note_id}: {type(e).__name__}: {e}")
            failed.append({"note_id": note_id, "error": f"{type(e).__name__}: {e}"})

        if i < len(note_ids) - 1:
            await asyncio.sleep(random.uniform(2, 5))

    return json_response({
        "notes": notes,
        "summary": {
            "total_requested": len(note_ids),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "from_cache": len(from_cache),
            "succeeded_ids": succeeded,
            "cached_ids": from_cache,
            "failed_details": failed,
        },
    })


async def handle_xhs_creator(ctx: HandlerContext, arguments: dict) -> list[TextContent]:
    """Handle xhs_creator tool call.

    Fetches profile info + recent notes for each user_id.
    Per-user caching with 7-day TTL.
    """
    user_ids: List[str] = arguments.get("user_ids", [])
    note_count: int = arguments.get("note_count", 5)
    force_refresh: bool = arguments.get("force_refresh", False)

    if not user_ids:
        return error_response("invalid_params", "user_ids is required and cannot be empty")

    try:
        client = await ctx.ensure_client()
    except (PlaywrightError, OSError, RuntimeError) as e:
        return error_response("browser_error", f"Failed to start browser: {e}")

    creators: List[dict] = []

    for i, user_id in enumerate(user_ids):
        user_cache_params = {"user_id": user_id, "note_count": note_count}

        if not force_refresh:
            cached_creator = ctx.cache_module.get("xhs_creator", user_cache_params)
            if cached_creator is not None:
                cached_creator["_from_cache"] = True
                creators.append(cached_creator)
                continue

        creator_data: Dict[str, Any] = {"user_id": user_id}

        # Step 1: Get profile info from HTML
        try:
            ctx.mark_request()
            profile = await client.get_creator_info(user_id)
            if profile:
                basic_info = profile.get("basicInfo", profile)
                interactions = profile.get("interactions", [])

                creator_data.update({
                    "nickname": basic_info.get("nickname", basic_info.get("nick_name", "")),
                    "desc": basic_info.get("desc", ""),
                    "avatar": basic_info.get(
                        "imageb",
                        basic_info.get("image", basic_info.get("avatar", "")),
                    ),
                    "ip_location": basic_info.get(
                        "ipLocation", basic_info.get("ip_location", "")
                    ),
                    "gender": basic_info.get("gender", ""),
                    "profile_raw": basic_info,
                })

                if interactions:
                    for item in interactions:
                        name = item.get("name", "")
                        count = item.get("count", "0")
                        if "粉丝" in name or "fans" in name.lower():
                            creator_data["fans"] = count
                        elif "关注" in name or "follow" in name.lower():
                            creator_data["follows"] = count
                        elif "赞" in name or "like" in name.lower():
                            creator_data["liked_and_collected"] = count

                tags = profile.get("tags", basic_info.get("tags", []))
                if tags:
                    creator_data["tags"] = [
                        t.get("name", t) if isinstance(t, dict) else str(t) for t in tags
                    ]
            else:
                creator_data["profile_error"] = "Could not parse profile page"
        except (httpx.HTTPError, PlaywrightError, KeyError, ValueError) as e:
            logger.error(f"Failed to get creator info for {user_id}: {type(e).__name__}: {e}")
            creator_data["profile_error"] = f"{type(e).__name__}: {e}"

        await asyncio.sleep(random.uniform(2, 5))

        # Step 2: Get recent notes
        try:
            ctx.mark_request()
            raw_notes = await client.get_creator_notes(user_id, max_count=note_count)
            recent_notes = []
            for note in raw_notes:
                recent_notes.append({
                    "note_id": note.get("note_id", ""),
                    "title": note.get("display_title", note.get("title", "")),
                    "desc": note.get("desc", ""),
                    "type": note.get("type", "normal"),
                    "time": note.get("time", 0),
                    "liked_count": note.get(
                        "liked_count",
                        note.get("interact_info", {}).get("liked_count", "0"),
                    ),
                    "xsec_token": note.get("xsec_token", ""),
                    "note_url": f"https://www.xiaohongshu.com/explore/{note.get('note_id', '')}",
                })
            creator_data["recent_notes"] = recent_notes
        except (httpx.HTTPError, PlaywrightError, KeyError, ValueError) as e:
            logger.error(f"Failed to get notes for creator {user_id}: {type(e).__name__}: {e}")
            creator_data["notes_error"] = f"{type(e).__name__}: {e}"
            creator_data["recent_notes"] = []

        if "profile_error" not in creator_data:
            ctx.cache_module.put("xhs_creator", user_cache_params, creator_data)

        creators.append(creator_data)

        if i < len(user_ids) - 1:
            await asyncio.sleep(random.uniform(5, 10))

    return json_response({"creators": creators})


async def handle_xhs_login(ctx: HandlerContext, arguments: dict) -> list[TextContent]:
    """Handle xhs_login tool call.

    Actions:
    - check: Verify current cookie validity
    - qrcode: Open visible browser for QR code scan
    - cookie_str: Import cookies from string
    """
    action: str = arguments.get("action", "check")
    cookie_str: str = arguments.get("cookie_str", "")
    browser_mgr = ctx.browser_mgr

    def _cookie_age_hours() -> Optional[float]:
        cache_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "cookies.json"
        )
        if not os.path.exists(cache_path):
            return None
        return round((time.time() - os.path.getmtime(cache_path)) / 3600, 1)

    if action == "check":
        try:
            if not browser_mgr.is_started:
                await browser_mgr.start()

            is_valid = await check_cookie_valid(browser_mgr)
            cookie_age = _cookie_age_hours()

            if is_valid:
                result: Dict[str, Any] = {
                    "status": "valid",
                    "message": "Cookie is valid, ready to use",
                }
            else:
                result = {
                    "status": "expired" if cookie_age is not None else "need_login",
                    "message": "Cookie expired or invalid. Please use action=qrcode to login.",
                }
            if cookie_age is not None:
                result["cookie_age_hours"] = cookie_age
            return json_response(result)

        except (httpx.HTTPError, PlaywrightError, OSError, RuntimeError) as e:
            return error_response(
                "check_failed", f"Failed to check login state: {type(e).__name__}: {e}"
            )

    elif action == "qrcode":
        try:
            if browser_mgr.is_started:
                await browser_mgr.restart(headless=False)
            else:
                await browser_mgr.start(headless=False)

            success = await login_by_qrcode(browser_mgr)
            if success:
                return json_response({
                    "status": "valid",
                    "message": "Login successful! Cookies saved.",
                })
            else:
                return json_response({
                    "status": "need_login",
                    "message": "Login timed out. Please try again.",
                })
        except (PlaywrightError, OSError, RuntimeError) as e:
            return error_response(
                "login_failed", f"QR code login failed: {type(e).__name__}: {e}"
            )

    elif action == "cookie_str":
        if not cookie_str:
            return error_response(
                "invalid_params", "cookie_str is required for cookie_str action"
            )

        try:
            if not browser_mgr.is_started:
                await browser_mgr.start()

            success = await login_by_cookie_str(browser_mgr, cookie_str)
            if success:
                is_valid = await check_cookie_valid(browser_mgr)
                if is_valid:
                    return json_response({
                        "status": "valid",
                        "message": "Cookies imported and verified successfully.",
                    })
                else:
                    return json_response({
                        "status": "expired",
                        "message": "Cookies imported but verification failed. "
                                   "They may be expired.",
                    })
            else:
                return error_response("import_failed", "Failed to import cookies.")
        except (httpx.HTTPError, PlaywrightError, OSError, RuntimeError) as e:
            return error_response(
                "login_failed", f"Cookie import failed: {type(e).__name__}: {e}"
            )

    else:
        return error_response(
            "invalid_params",
            f"Unknown action: {action}. Use check, qrcode, or cookie_str.",
        )


async def handle_xhs_status(ctx: HandlerContext, arguments: dict) -> list[TextContent]:
    """Handle xhs_status tool call."""
    browser_mgr = ctx.browser_mgr
    result: Dict[str, Any] = {
        "server": "running",
        "browser": "connected" if browser_mgr.is_started else "disconnected",
        "last_request_time": ctx.last_request_time,
    }

    if browser_mgr.is_started:
        try:
            is_valid = await check_cookie_valid(browser_mgr)
            result["cookie"] = "valid" if is_valid else "expired"
        except httpx.HTTPError as e:
            result["cookie"] = "network_error"
            result["cookie_error_detail"] = f"{type(e).__name__}: {e}"
            logger.warning(f"Cookie check network failure: {type(e).__name__}: {e}")
        except PlaywrightError as e:
            result["cookie"] = "browser_error"
            result["cookie_error_detail"] = f"{type(e).__name__}: {e}"
            logger.warning(f"Cookie check browser failure: {type(e).__name__}: {e}")
        except Exception as e:  # last-resort fallback, still structured + logged
            result["cookie"] = "error"
            result["cookie_error_detail"] = f"{type(e).__name__}: {e}"
            logger.exception(f"Cookie check unexpected failure: {e}")

        a1 = browser_mgr.cookie_dict.get("a1")
        result["has_a1_cookie"] = bool(a1)
    else:
        result["cookie"] = "unknown (browser not started)"

    cache_stats = ctx.cache_module.get_stats()
    result["cache_entries"] = cache_stats["cache_entries"]
    result["cache_size_mb"] = cache_stats["cache_size_mb"]

    cookie_cache = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "cookies.json"
    )
    result["cookie_cache_exists"] = os.path.exists(cookie_cache)
    if os.path.exists(cookie_cache):
        mtime = os.path.getmtime(cookie_cache)
        result["cookie_cache_age_hours"] = round((time.time() - mtime) / 3600, 1)

    return json_response(result)


# Dispatch table used by server.py's call_tool handler.
HANDLERS = {
    "xhs_search": handle_xhs_search,
    "xhs_detail": handle_xhs_detail,
    "xhs_creator": handle_xhs_creator,
    "xhs_login": handle_xhs_login,
    "xhs_status": handle_xhs_status,
}
