# xhs-mcp configuration
# Author: Wang
# License: Non-Commercial Learning Use Only

# Browser settings
HEADLESS = False
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# XHS endpoints
XHS_INDEX_URL = "https://www.xiaohongshu.com"
XHS_API_HOST = "https://edith.xiaohongshu.com"

# Request defaults
REQUEST_TIMEOUT = 60
CRAWL_INTERVAL_MIN = 2.0
CRAWL_INTERVAL_MAX = 5.0

# Cookie cache file
COOKIE_CACHE_PATH = "config/cookies.json"

# Default headers template
DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.xiaohongshu.com",
    "pragma": "no-cache",
    "referer": "https://www.xiaohongshu.com/",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}
