# xhs-mcp data models and exceptions
# Author: Wang
# Extracted and simplified from MediaCrawler
# License: Non-Commercial Learning Use Only

from enum import Enum

from httpx import RequestError


# ── Enums ──

class SearchSortType(Enum):
    """Search result sort type"""
    GENERAL = "general"
    MOST_POPULAR = "popularity_descending"
    LATEST = "time_descending"


class SearchNoteType(Enum):
    """Search note type filter"""
    ALL = 0
    VIDEO = 1
    IMAGE = 2


# ── Exceptions ──

class DataFetchError(RequestError):
    """Error when fetching data from XHS API"""


class IPBlockError(RequestError):
    """IP has been blocked by XHS"""


class NoteNotFoundError(RequestError):
    """Note does not exist or is in abnormal state"""
