"""
session_manager.py
Manages the "_t" lookup tokens that infolookup.site requires on /api/tcpa
(and /api/lookup, /api/save-info).

What we learned by reading their JS (test2.js) and testing directly:
  - GET /lookup-token.php is PUBLIC and unauthenticated — no login, no
    captcha. It returns {"status":"ok","token":"...","expires":<unix ts>}.
  - The token is a base64 blob of "<timestamp>|<requesting IP>.<hash>" —
    i.e. it's bound to whichever IP address requested it, and it's only
    valid for ~15 minutes.
  - Calling /api/tcpa with a token minted from a DIFFERENT IP than the one
    making that call returns HTTP 403 {"status":"error","message":"Access
    denied."} even though the token itself is fresh and otherwise valid.

Because of that IP binding, this is NOT a single global token like a normal
API key — it's "one token per outbound network path". So this manager keys
everything by proxy address (None = the direct/no-proxy connection) and
keeps ONE requests.Session per key, reusing that same session's connection
pool for both minting the token and calling the API. That maximizes the
chance the mint request and the lookup request share the same egress IP —
which is exactly what has to be true for this to work.

If you run entirely without proxies (the common case), there's just one
key ("direct") and this behaves like a normal shared-token manager that
auto-refreshes every ~14 minutes. If you use proxies, each proxy gets its
own token, minted through itself.
"""

import logging
import threading
import time

import requests

from backend.config import BASE_HEADERS, TCPA_CHECK_URL, TOKEN_REFRESH_MARGIN_SECS, TOKEN_URL

logger = logging.getLogger(__name__)

DIRECT_KEY = "direct"


class _TokenEntry:
    __slots__ = ("token", "expires", "http")

    def __init__(self, token: str, expires: float, http: requests.Session):
        self.token = token
        self.expires = expires
        self.http = http


class SessionManager:
    """Thread-safe, per-proxy token broker for infolookup.site."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, _TokenEntry] = {}
        self._mint_locks: dict[str, threading.Lock] = {}
        self._last_error: str | None = None
        self._manual_override: str | None = None  # optional escape hatch

    @staticmethod
    def _key(proxy: str | None) -> str:
        return proxy or DIRECT_KEY

    def _mint_lock_for(self, key: str) -> threading.Lock:
        with self._lock:
            if key not in self._mint_locks:
                self._mint_locks[key] = threading.Lock()
            return self._mint_locks[key]

    # ── manual override (rarely needed — see README) ──────────

    def set_manual_token(self, raw_input: str) -> bool:
        """Force a specific token for ALL requests, bypassing auto-mint.
        Only useful if infolookup.site ever locks down /lookup-token.php —
        as of writing it's public, so you normally never need this."""
        raw = (raw_input or "").strip()
        self._manual_override = raw or None
        if raw:
            logger.info(f"[Session] Manual token override set ({raw[:24]}...)")
        else:
            logger.info("[Session] Manual token override cleared.")
        return bool(raw)

    # ── main entry point used by the checker engine ───────────

    def get_session_and_token(
        self, proxy: str | None, proxies_dict: dict | None
    ) -> tuple[requests.Session, str] | tuple[None, None]:
        """Return (session, token) to use for a request through the given
        proxy (None = direct). Mints/refreshes the token if needed."""
        if self._manual_override:
            return requests.Session(), self._manual_override

        key = self._key(proxy)
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.expires - time.time() > TOKEN_REFRESH_MARGIN_SECS:
                return entry.http, entry.token

        # Refresh outside the main lock so other proxy-keys aren't blocked,
        # but serialize refreshes for the SAME key so threads sharing one
        # proxy don't all mint tokens at once.
        with self._mint_lock_for(key):
            with self._lock:
                entry = self._entries.get(key)
                if entry and entry.expires - time.time() > TOKEN_REFRESH_MARGIN_SECS:
                    return entry.http, entry.token
            return self._mint(key, proxies_dict)

    def invalidate(self, proxy: str | None):
        """Force the next request on this proxy to mint a fresh token —
        called when the API rejects a token as expired mid-run."""
        key = self._key(proxy)
        with self._lock:
            self._entries.pop(key, None)
        logger.info(f"[Session] Invalidated cached token for '{key}'.")

    def test_connection(self, proxy: str | None = None, proxies_dict: dict | None = None) -> dict:
        """Mint a token and fire one real probe request — used by the
        'Test connection' button so you can confirm end-to-end reachability
        before starting a bulk run. Pass proxies_dict from ProxyManager.
        to_requests_dict() when testing a specific (possibly authenticated)
        proxy — a bare proxy string here won't include credentials."""
        if proxy and proxies_dict is None:
            proxies_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        session, token = self.get_session_and_token(proxy, proxies_dict)
        if not token:
            return {"ok": False, "message": self._last_error or "Could not obtain a lookup token."}

        try:
            resp = session.get(
                TCPA_CHECK_URL,
                params={"x": "8065551234", "_t": token, "pi": "1"},
                headers=BASE_HEADERS,
                proxies=proxies_dict,
                timeout=10,
            )
            logger.info(f"[Session] Test connection -> HTTP {resp.status_code}")

            if resp.status_code in (401, 403):
                self.invalidate(proxy)
                return {"ok": False, "message": f"HTTP {resp.status_code} — token rejected (likely IP mismatch or expired). Retrying will mint a fresh one."}
            if resp.status_code == 429:
                return {"ok": False, "message": "HTTP 429 — rate limited right now, try again shortly."}
            if resp.status_code != 200:
                return {"ok": False, "message": f"Unexpected HTTP {resp.status_code}."}

            try:
                data = resp.json()
            except ValueError:
                return {"ok": False, "message": "HTTP 200 but response wasn't valid JSON."}

            return {"ok": True, "message": "Token + API reachable — got a real response.", "sample": data}

        except requests.exceptions.RequestException as e:
            logger.error(f"[Session] Test connection network error: {e}")
            return {"ok": False, "message": f"Network error: {e}"}

    # ── internals ──────────────────────────────────────────────

    def _mint(self, key: str, proxies_dict: dict | None) -> tuple[requests.Session, str] | tuple[None, None]:
        http = requests.Session()
        try:
            resp = http.get(TOKEN_URL, headers=BASE_HEADERS, proxies=proxies_dict, timeout=12)
            if resp.status_code != 200:
                self._last_error = f"Token endpoint returned HTTP {resp.status_code}."
                logger.error(f"[Session] {self._last_error} (key={key})")
                return None, None

            data = resp.json()
            if data.get("status") != "ok" or not data.get("token"):
                self._last_error = f"Token endpoint returned unexpected payload: {data}"
                logger.error(f"[Session] {self._last_error} (key={key})")
                return None, None

            token = data["token"]
            expires = float(data.get("expires") or (time.time() + 600))
            with self._lock:
                self._entries[key] = _TokenEntry(token, expires, http)
            self._last_error = None
            logger.info(f"[Session] Minted token for '{key}', valid ~{expires - time.time():.0f}s.")
            return http, token

        except requests.exceptions.RequestException as e:
            self._last_error = f"Network error minting token: {e}"
            logger.error(f"[Session] {self._last_error} (key={key})")
            return None, None
        except Exception as e:
            self._last_error = f"Unexpected error minting token: {e}"
            logger.error(f"[Session] {self._last_error} (key={key})")
            return None, None

    # ── accessors for the UI ────────────────────────────────────

    def is_valid(self, proxy: str | None = None) -> bool:
        if self._manual_override:
            return True
        key = self._key(proxy)
        with self._lock:
            entry = self._entries.get(key)
            return bool(entry and entry.expires > time.time())

    def refresh(self, proxy: str | None = None, proxies_dict: dict | None = None) -> bool:
        """Force-mint a token now (used by the sidebar's manual refresh button)."""
        if proxy and proxies_dict is None:
            proxies_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        session, token = self._mint(self._key(proxy), proxies_dict)
        return token is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def active_keys(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())

    def token_preview(self, proxy: str | None = None) -> str:
        if self._manual_override:
            return self._manual_override[:32] + "..."
        key = self._key(proxy)
        with self._lock:
            entry = self._entries.get(key)
            return (entry.token[:32] + "...") if entry else "None"
