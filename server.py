# xhs-mcp - Xiaohongshu MCP Server
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# MCP Server entry point. Registers all XHS tools and dispatches
# tool calls to the appropriate handler functions.

import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

from xhs.browser import BrowserManager
from xhs import cache as request_cache
from xhs.client import XHSClient
from xhs.login import login_by_qrcode, login_by_cookie_str, check_cookie_valid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("xhs-mcp")

# ── Global state ──
server = Server("xhs")
browser_mgr = BrowserManager()
_last_request_time: Optional[str] = None


def _json_response(data: dict) -> list[TextContent]:
    """Wrap a dict as MCP TextContent JSON response."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def _error_response(error_code: str, message: str) -> list[TextContent]:
    """Return a structured error response (never throws)."""
    return _json_response({"error": error_code, "message": message})


async def _ensure_client() -> XHSClient:
    """Ensure browser is started and return an XHSClient."""
    if not browser_mgr.is_started:
        await browser_mgr.start()
    return XHSClient(browser_mgr)


def _update_last_request_time():
    global _last_request_time
    _last_request_time = datetime.now().isoformat()


# ── Search result formatting ──

def _format_search_note(item: dict) -> dict:
    """Extract structured note info from a raw search result item."""
    note_card = item.get("note_card", {})
    user_info = note_card.get("user", {})
    interact_info = note_card.get("interact_info", {})

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
        "collected_count": interact_info.get("collected_count", note_card.get("collected_count", "0")),
        "comment_count": interact_info.get("comment_count", note_card.get("comment_count", "0")),
        "share_count": interact_info.get("share_count", note_card.get("share_count", "0")),
        "tag_list": [t.get("name", "") for t in note_card.get("tag_list", [])],
        "image_list": [
            img.get("url_default", img.get("url", ""))
            for img in note_card.get("image_list", [])
        ],
        "note_url": f"https://www.xiaohongshu.com/explore/{item.get('id', '')}",
    }


def _format_note_detail(note_card: dict) -> dict:
    """Format a note detail response."""
    user_info = note_card.get("user", {})
    interact_info = note_card.get("interact_info", {})

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
        "collected_count": interact_info.get("collected_count", note_card.get("collected_count", "0")),
        "comment_count": interact_info.get("comment_count", note_card.get("comment_count", "0")),
        "share_count": interact_info.get("share_count", note_card.get("share_count", "0")),
        "tag_list": [t.get("name", "") for t in note_card.get("tag_list", [])],
        "image_list": [
            img.get("url_default", img.get("url", ""))
            for img in note_card.get("image_list", [])
        ],
        "note_url": f"https://www.xiaohongshu.com/explore/{note_card.get('note_id', '')}",
    }


def _format_comment(comment: dict) -> dict:
    """Format a single comment."""
    user = comment.get("user_info", {})
    sub_comments_raw = comment.get("sub_comments", [])
    sub_comments = []
    for sc in sub_comments_raw:
        sc_user = sc.get("user_info", {})
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

async def _handle_xhs_search(arguments: dict) -> list[TextContent]:
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
        return _error_response("invalid_params", "keywords is required and cannot be empty")

    # Cache: check whole-call cache (all keywords + params combined)
    cache_params = {"keywords": sorted(keywords), "sort": sort, "page": page, "note_type": note_type}
    if not force_refresh:
        cached = request_cache.get("xhs_search", cache_params)
        if cached is not None:
            cached["_from_cache"] = True
            return _json_response(cached)

    try:
        client = await _ensure_client()
    except Exception as e:
        return _error_response("browser_error", f"Failed to start browser: {e}")

    all_notes = []
    keyword_results = {}

    for i, keyword in enumerate(keywords):
        try:
            _update_last_request_time()
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
                notes.append(_format_search_note(item))

            keyword_results[keyword] = {
                "count": len(notes),
                "has_more": raw.get("has_more", False),
            }
            all_notes.extend(notes)

        except Exception as e:
            logger.error(f"Search failed for keyword '{keyword}': {e}")
            keyword_results[keyword] = {
                "count": 0,
                "has_more": False,
                "error": str(e),
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
    request_cache.put("xhs_search", cache_params, result)

    return _json_response(result)


async def _handle_xhs_detail(arguments: dict) -> list[TextContent]:
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
        return _error_response("invalid_params", "note_ids is required and cannot be empty")
    if not xsec_tokens or len(xsec_tokens) != len(note_ids):
        return _error_response(
            "invalid_params",
            f"xsec_tokens must be provided and match note_ids length (got {len(note_ids)} ids, {len(xsec_tokens)} tokens)"
        )

    try:
        client = await _ensure_client()
    except Exception as e:
        return _error_response("browser_error", f"Failed to start browser: {e}")

    notes = []
    succeeded = []
    failed = []
    from_cache = []

    for i, (note_id, xsec_token) in enumerate(zip(note_ids, xsec_tokens)):
        # Per-note cache key
        note_cache_params = {"note_id": note_id, "get_comments": get_comments, "comment_count": comment_count}

        # Check per-note cache
        if not force_refresh:
            cached_note = request_cache.get("xhs_detail", note_cache_params)
            if cached_note is not None:
                notes.append(cached_note)
                succeeded.append(note_id)
                from_cache.append(note_id)
                continue

        try:
            _update_last_request_time()
            note_card = await client.get_note_by_id(
                note_id=note_id,
                xsec_token=xsec_token,
            )

            if not note_card:
                failed.append({"note_id": note_id, "error": "empty_response"})
                continue

            formatted = _format_note_detail(note_card)

            # Fetch comments if requested
            if get_comments:
                # 2-5s interval before comment fetch
                await asyncio.sleep(random.uniform(2, 5))
                _update_last_request_time()
                try:
                    raw_comments = await client.get_note_all_comments(
                        note_id=note_id,
                        xsec_token=xsec_token,
                        max_count=comment_count,
                    )
                    formatted["comments"] = [_format_comment(c) for c in raw_comments]
                except Exception as ce:
                    logger.warning(f"Failed to get comments for {note_id}: {ce}")
                    formatted["comments"] = []
                    formatted["comments_error"] = str(ce)

            notes.append(formatted)
            succeeded.append(note_id)

            # Cache this note
            request_cache.put("xhs_detail", note_cache_params, formatted)

        except Exception as e:
            logger.error(f"Failed to get detail for {note_id}: {e}")
            failed.append({"note_id": note_id, "error": str(e)})

        # 2-5s interval between notes (not after the last one)
        if i < len(note_ids) - 1:
            await asyncio.sleep(random.uniform(2, 5))

    return _json_response({
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


async def _handle_xhs_creator(arguments: dict) -> list[TextContent]:
    """Handle xhs_creator tool call.

    Fetches profile info + recent notes for each user_id.
    Per-user caching with 7-day TTL.
    """
    user_ids: List[str] = arguments.get("user_ids", [])
    note_count: int = arguments.get("note_count", 5)
    force_refresh: bool = arguments.get("force_refresh", False)

    if not user_ids:
        return _error_response("invalid_params", "user_ids is required and cannot be empty")

    try:
        client = await _ensure_client()
    except Exception as e:
        return _error_response("browser_error", f"Failed to start browser: {e}")

    creators = []

    for i, user_id in enumerate(user_ids):
        # Per-user cache key
        user_cache_params = {"user_id": user_id, "note_count": note_count}

        if not force_refresh:
            cached_creator = request_cache.get("xhs_creator", user_cache_params)
            if cached_creator is not None:
                cached_creator["_from_cache"] = True
                creators.append(cached_creator)
                continue

        creator_data = {"user_id": user_id}

        # Step 1: Get profile info from HTML
        try:
            _update_last_request_time()
            profile = await client.get_creator_info(user_id)
            if profile:
                # Extract key fields (structure may vary)
                basic_info = profile.get("basicInfo", profile)
                interactions = profile.get("interactions", [])

                creator_data.update({
                    "nickname": basic_info.get("nickname", basic_info.get("nick_name", "")),
                    "desc": basic_info.get("desc", ""),
                    "avatar": basic_info.get("imageb", basic_info.get("image", basic_info.get("avatar", ""))),
                    "ip_location": basic_info.get("ipLocation", basic_info.get("ip_location", "")),
                    "gender": basic_info.get("gender", ""),
                    "profile_raw": basic_info,  # Include raw for Claude to analyze
                })

                # Parse interaction counts if available
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

                # Try to get tags
                tags = profile.get("tags", basic_info.get("tags", []))
                if tags:
                    creator_data["tags"] = [
                        t.get("name", t) if isinstance(t, dict) else str(t)
                        for t in tags
                    ]
            else:
                creator_data["profile_error"] = "Could not parse profile page"
        except Exception as e:
            logger.error(f"Failed to get creator info for {user_id}: {e}")
            creator_data["profile_error"] = str(e)

        # 2-5s interval
        await asyncio.sleep(random.uniform(2, 5))

        # Step 2: Get recent notes
        try:
            _update_last_request_time()
            raw_notes = await client.get_creator_notes(user_id, max_count=note_count)
            recent_notes = []
            for note in raw_notes:
                recent_notes.append({
                    "note_id": note.get("note_id", ""),
                    "title": note.get("display_title", note.get("title", "")),
                    "desc": note.get("desc", ""),
                    "type": note.get("type", "normal"),
                    "time": note.get("time", 0),
                    "liked_count": note.get("liked_count", note.get("interact_info", {}).get("liked_count", "0")),
                    "xsec_token": note.get("xsec_token", ""),
                    "note_url": f"https://www.xiaohongshu.com/explore/{note.get('note_id', '')}",
                })
            creator_data["recent_notes"] = recent_notes
        except Exception as e:
            logger.error(f"Failed to get notes for creator {user_id}: {e}")
            creator_data["notes_error"] = str(e)
            creator_data["recent_notes"] = []

        # Cache this creator (only if no profile error)
        if "profile_error" not in creator_data:
            request_cache.put("xhs_creator", user_cache_params, creator_data)

        creators.append(creator_data)

        # 5-10s gap between different creators (not after the last one)
        if i < len(user_ids) - 1:
            await asyncio.sleep(random.uniform(5, 10))

    return _json_response({"creators": creators})


async def _handle_xhs_login(arguments: dict) -> list[TextContent]:
    """Handle xhs_login tool call.

    Actions:
    - check: Verify current cookie validity
    - qrcode: Open visible browser for QR code scan
    - cookie_str: Import cookies from string
    """
    action: str = arguments.get("action", "check")
    cookie_str: str = arguments.get("cookie_str", "")

    if action == "check":
        try:
            # Start browser if needed (headless is fine for check)
            if not browser_mgr.is_started:
                await browser_mgr.start()

            is_valid = await check_cookie_valid(browser_mgr)
            cookie_age = None

            # Check cookie cache file age
            cache_path = os.path.join(os.path.dirname(__file__), "config", "cookies.json")
            if os.path.exists(cache_path):
                mtime = os.path.getmtime(cache_path)
                cookie_age = round((time.time() - mtime) / 3600, 1)

            if is_valid:
                result = {
                    "status": "valid",
                    "message": "Cookie is valid, ready to use",
                }
                if cookie_age is not None:
                    result["cookie_age_hours"] = cookie_age
            else:
                result = {
                    "status": "expired" if cookie_age is not None else "need_login",
                    "message": "Cookie expired or invalid. Please use action=qrcode to login."
                }
                if cookie_age is not None:
                    result["cookie_age_hours"] = cookie_age

            return _json_response(result)

        except Exception as e:
            return _error_response("check_failed", f"Failed to check login state: {e}")

    elif action == "qrcode":
        try:
            # QR code login MUST use headless=False
            if browser_mgr.is_started:
                # Restart with visible browser
                await browser_mgr.restart(headless=False)
            else:
                await browser_mgr.start(headless=False)

            success = await login_by_qrcode(browser_mgr)
            if success:
                return _json_response({
                    "status": "valid",
                    "message": "Login successful! Cookies saved.",
                })
            else:
                return _json_response({
                    "status": "need_login",
                    "message": "Login timed out. Please try again.",
                })
        except Exception as e:
            return _error_response("login_failed", f"QR code login failed: {e}")

    elif action == "cookie_str":
        if not cookie_str:
            return _error_response("invalid_params", "cookie_str is required for cookie_str action")

        try:
            if not browser_mgr.is_started:
                await browser_mgr.start()

            success = await login_by_cookie_str(browser_mgr, cookie_str)
            if success:
                # Verify the imported cookies work
                is_valid = await check_cookie_valid(browser_mgr)
                if is_valid:
                    return _json_response({
                        "status": "valid",
                        "message": "Cookies imported and verified successfully.",
                    })
                else:
                    return _json_response({
                        "status": "expired",
                        "message": "Cookies imported but verification failed. They may be expired.",
                    })
            else:
                return _error_response("import_failed", "Failed to import cookies.")
        except Exception as e:
            return _error_response("login_failed", f"Cookie import failed: {e}")

    else:
        return _error_response("invalid_params", f"Unknown action: {action}. Use check, qrcode, or cookie_str.")


async def _handle_xhs_status(arguments: dict) -> list[TextContent]:
    """Handle xhs_status tool call."""
    result = {
        "server": "running",
        "browser": "connected" if browser_mgr.is_started else "disconnected",
        "last_request_time": _last_request_time,
    }

    # Cookie status
    if browser_mgr.is_started:
        try:
            is_valid = await check_cookie_valid(browser_mgr)
            result["cookie"] = "valid" if is_valid else "expired"
        except Exception:
            result["cookie"] = "error"

        # Check a1 presence
        a1 = browser_mgr.cookie_dict.get("a1")
        result["has_a1_cookie"] = bool(a1)
    else:
        result["cookie"] = "unknown (browser not started)"

    # Request cache stats
    cache_stats = request_cache.get_stats()
    result["cache_entries"] = cache_stats["cache_entries"]
    result["cache_size_mb"] = cache_stats["cache_size_mb"]

    # Cookie cache file
    cookie_cache = os.path.join(os.path.dirname(__file__), "config", "cookies.json")
    result["cookie_cache_exists"] = os.path.exists(cookie_cache)
    if os.path.exists(cookie_cache):
        mtime = os.path.getmtime(cookie_cache)
        result["cookie_cache_age_hours"] = round((time.time() - mtime) / 3600, 1)

    return _json_response(result)


# ══════════════════════════════════════════════════════════════
# Tool registration
# ══════════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="xhs_search",
            description="Search Xiaohongshu notes by keyword. Returns notes with titles, stats, and xsec_token for further detail fetching.",
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search keywords (required). Multiple keywords are searched sequentially with 5-10s gaps.",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["general", "time_descending", "popularity_descending"],
                        "default": "general",
                        "description": "Sort order: general (default), time_descending (latest), popularity_descending (most popular)",
                    },
                    "page": {
                        "type": "integer",
                        "default": 1,
                        "description": "Page number",
                    },
                    "note_type": {
                        "type": "integer",
                        "enum": [0, 1, 2],
                        "default": 0,
                        "description": "0=all, 1=video only, 2=image only",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass cache and force fresh request",
                    },
                },
                "required": ["keywords"],
            },
        ),
        Tool(
            name="xhs_detail",
            description="Get full content of Xiaohongshu notes (text, images, comments). Requires note_ids and xsec_tokens from xhs_search results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Note IDs to fetch (required)",
                    },
                    "xsec_tokens": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Corresponding xsec_tokens from search results (required, same order as note_ids)",
                    },
                    "get_comments": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to fetch comments",
                    },
                    "comment_count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max comments per note (only used when get_comments=true)",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass cache and force fresh request",
                    },
                },
                "required": ["note_ids", "xsec_tokens"],
            },
        ),
        Tool(
            name="xhs_creator",
            description="Get a Xiaohongshu user's profile info and recent posts. Useful for account credibility analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "User IDs to look up (required)",
                    },
                    "note_count": {
                        "type": "integer",
                        "default": 5,
                        "description": "Number of recent notes to fetch per creator",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass cache and force fresh request",
                    },
                },
                "required": ["user_ids"],
            },
        ),
        Tool(
            name="xhs_login",
            description="Manage Xiaohongshu login. Use action=check to verify, action=qrcode to scan QR code (opens visible browser), action=cookie_str to import cookies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["check", "qrcode", "cookie_str"],
                        "default": "check",
                        "description": "Login action: check (verify current state), qrcode (scan to login), cookie_str (import cookie string)",
                    },
                    "cookie_str": {
                        "type": "string",
                        "default": "",
                        "description": "Cookie string from browser devtools (only for action=cookie_str)",
                    },
                },
            },
        ),
        Tool(
            name="xhs_status",
            description="Check XHS MCP Server status: browser connection, cookie validity, cache size, last request time.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to handler functions."""
    handlers = {
        "xhs_search": _handle_xhs_search,
        "xhs_detail": _handle_xhs_detail,
        "xhs_creator": _handle_xhs_creator,
        "xhs_login": _handle_xhs_login,
        "xhs_status": _handle_xhs_status,
    }

    handler = handlers.get(name)
    if handler is None:
        return _error_response("unknown_tool", f"Unknown tool: {name}")

    try:
        return await handler(arguments)
    except Exception as e:
        logger.error(f"Unhandled error in tool {name}: {e}", exc_info=True)
        return _error_response("internal_error", f"Unexpected error: {e}")


# ── Entry point ──

async def run():
    logger.info("Starting xhs-mcp server...")
    try:
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await browser_mgr.stop()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
