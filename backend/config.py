"""
config.py
Central place for every constant that might need to change later -
target API, headers, timeouts, folders. If the client's endpoint or
token scheme ever changes, this is the only file that should need edits.
"""

from pathlib import Path

# ── target API ───────────────────────────────────────────────
# This is the same TCPA/DNC lookup endpoint the previous tool used.
# If your friend's captured request used a different host/path, update it here.
API_BASE_URL = "https://api.uspeoplesearch.net"
TCPA_CHECK_URL = f"{API_BASE_URL}/tcpa/v1"
SESSION_VALIDATE_URL = f"{API_BASE_URL}/auth/session/validate"
HOME_URL = "https://uspeoplesearch.net/"

# ── request headers ──────────────────────────────────────────
# Mirrors a real browser call as closely as possible. The session token
# gets merged in at request time under X-Session-ID.
BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Origin": "https://uspeoplesearch.net",
    "Referer": "https://uspeoplesearch.net/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
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
