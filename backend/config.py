"""
config.py
Central place for every constant that might need to change later -
target API, headers, timeouts, folders. If the client's endpoint or
token scheme ever changes, this is the only file that should need edits.
"""

from pathlib import Path

# ── target site ──────────────────────────────────────────────
# infolookup.site's own frontend (test2.js) mints a short-lived, IP-bound
# lookup token from /lookup-token.php (fully public, no login/captcha) and
# then uses it to call /api/tcpa. This is confirmed by reading their JS
# directly and testing both endpoints. See session_manager.py for why the
# token is IP-bound and what that means for proxy usage.
API_BASE_URL = "https://infolookup.site"
TOKEN_URL = f"{API_BASE_URL}/lookup-token.php"
TCPA_CHECK_URL = f"{API_BASE_URL}/api/tcpa"
HOME_URL = f"{API_BASE_URL}/"

# How much time (seconds) before a token's real expiry we treat it as
# "needs refresh" — matches the margin the site's own JS uses.
TOKEN_REFRESH_MARGIN_SECS = 30

# ── request headers ──────────────────────────────────────────
# Mirrors a real browser call as closely as possible.
BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": HOME_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# ── timing / retry defaults ──────────────────────────────────
REQUEST_TIMEOUT_SECS = 8
MAX_RETRIES_PER_NUMBER = 3

# ── folders ───────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
PROXIES_FILE = ROOT_DIR / "proxies.txt"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "app.log"
