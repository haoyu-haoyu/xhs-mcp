# xhs-mcp - Xiaohongshu MCP Server
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# MCP Server entry point.  Owns MCP tool registration + dispatch only.
# The actual per-tool logic lives in ``xhs.handlers`` so the handlers
# can be unit-tested without spinning up the MCP stdio transport.

import asyncio
import logging
import sys

from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

from xhs.browser import BrowserManager
from xhs.handlers import HANDLERS, HandlerContext, error_response

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
handler_ctx = HandlerContext(browser_mgr=browser_mgr)


# ══════════════════════════════════════════════════════════════
# Tool registration
# ══════════════════════════════════════════════════════════════


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="xhs_search",
            description=(
                "Search Xiaohongshu notes by keyword. Returns notes with titles, "
                "stats, and xsec_token for further detail fetching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Search keywords (required). Multiple keywords are "
                            "searched sequentially with 5-10s gaps."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["general", "time_descending", "popularity_descending"],
                        "default": "general",
                        "description": (
                            "Sort order: general (default), time_descending (latest), "
                            "popularity_descending (most popular)"
                        ),
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
            description=(
                "Get full content of Xiaohongshu notes (text, images, comments). "
                "Requires note_ids and xsec_tokens from xhs_search results."
            ),
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
                        "description": (
                            "Corresponding xsec_tokens from search results "
                            "(required, same order as note_ids)"
                        ),
                    },
                    "get_comments": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to fetch comments",
                    },
                    "comment_count": {
                        "type": "integer",
                        "default": 20,
                        "description": (
                            "Max comments per note (only used when get_comments=true)"
                        ),
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
            description=(
                "Get a Xiaohongshu user's profile info and recent posts. "
                "Useful for account credibility analysis."
            ),
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
            description=(
                "Manage Xiaohongshu login. Use action=check to verify, "
                "action=qrcode to scan QR code (opens visible browser), "
                "action=cookie_str to import cookies."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["check", "qrcode", "cookie_str"],
                        "default": "check",
                        "description": (
                            "Login action: check (verify current state), "
                            "qrcode (scan to login), cookie_str (import cookie string)"
                        ),
                    },
                    "cookie_str": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Cookie string from browser devtools "
                            "(only for action=cookie_str)"
                        ),
                    },
                },
            },
        ),
        Tool(
            name="xhs_status",
            description=(
                "Check XHS MCP Server status: browser connection, cookie validity, "
                "cache size, last request time."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to handler functions in ``xhs.handlers``."""
    handler = HANDLERS.get(name)
    if handler is None:
        return error_response("unknown_tool", f"Unknown tool: {name}")

    try:
        return await handler(handler_ctx, arguments)
    except Exception as e:  # last-resort safety net for the MCP protocol
        logger.error(f"Unhandled error in tool {name}: {e}", exc_info=True)
        return error_response("internal_error", f"Unexpected error: {e}")


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
