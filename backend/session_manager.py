"""
session_manager.py
Handles the session/auth token needed to call the TCPA API.

Why manual paste is the PRIMARY path here (not just a fallback):
The site fronts its API with bot-protection (Cloudflare Turnstile / similar),
which is almost certainly why the old tool "sometimes just doesn't work" -
an automated fetch of the homepage can get a challenge page instead of a
usable session cookie, silently, with no obvious error. Rather than fight
that blind, this manager:
  1) lets you paste a token/cookie captured from a real browser (DevTools ->
     Network tab -> any api.uspeoplesearch.net request -> copy the
     X-Session-ID header or the session cookie value) - this always works
     because it came from a real, already-solved browser session.
  2) still offers an automatic refresh attempt, for when the site happens
     to serve the homepage without a challenge.
  3) exposes test_connection() so you can verify a token actually works
     against the real API with one click, before burning time on a bulk run.
"""

import json
import logging
import threading
import time

import requests

from backend.config import (
    BASE_HEADERS,
    HOME_URL,
    SESSION_VALIDATE_URL,
    TCPA_CHECK_URL,
    REQUEST_TIMEOUT_SECS,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """Thread-safe holder for the current session token."""

    def __init__(self):
        self._token: str | None = None
        self._lock = threading.Lock()
        self._last_fetch: float = 0.0
        self._http = requests.Session()
        self._refresh_count = 0
        self._last_error: str | None = None
        self._source: str = "none"  # "manual" or "auto"

    # ── setting the token ─────────────────────────────────────

    def set_token(self, raw_input: str) -> bool:
        """
        Accept a manually pasted value. Understands three shapes:
          - a bare token string, e.g.  a1b2c3...
          - a raw 'Cookie:' header line
          - a JSON blob like {"session_id": "..."} pasted from devtools
        Returns True if a token was extracted and applied.
        """
        raw = (raw_input or "").strip()
        token = None

        if raw:
            # Try JSON first.
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for key in ("session_id", "token", "session", "X-Session-ID"):
                        val = parsed.get(key)
                        if isinstance(val, str) and val.strip():
                            token = val.strip()
                            break
            except (json.JSONDecodeError, TypeError):
                pass

            # Try "key=value; key2=value2" cookie-header style.
            if token is None and "=" in raw and ";" in raw:
                for part in raw.split(";"):
                    if "=" in part:
                        k, _, v = part.strip().partition("=")
                        if "session" in k.lower() and v.strip():
                            token = v.strip()
                            break

            # Otherwise treat the whole input as the raw token.
            if token is None:
                token = raw

        with self._lock:
            if token:
                self._token = token
                self._last_fetch = time.time()
                self._last_error = None
                self._source = "manual"
                logger.info(f"[Session] Manual token applied ({token[:24]}...)")
                return True
            self._token = None
            self._source = "none"
            logger.info("[Session] Token cleared.")
            return False

    # ── automatic refresh (best-effort) ───────────────────────

    def refresh(self, max_retries: int = 2) -> bool:
        """Try to auto-fetch a session cookie from the homepage. Best-effort
        only - if the site serves a bot-check page this will fail cleanly
        and tell you to paste a token manually instead."""
        self._last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"[Session] Auto-refresh attempt {attempt}/{max_retries}")
                home = self._http.get(HOME_URL, headers=BASE_HEADERS, timeout=12)
                body = (home.text or "").lower()

                if any(x in body for x in ("turnstile", "cf-challenge", "just a moment", "captcha")):
                    self._last_error = (
                        "Site returned a bot-check page (Turnstile/Cloudflare). "
                        "Auto-refresh can't get past this - open the site in a real "
                        "browser, solve it once, then paste the session token manually."
                    )
                    logger.warning("[Session] Bot-check page detected during auto-refresh.")
                    return False

                raw_cookie = self._extract_session_cookie()
                if not raw_cookie:
                    logger.warning("[Session] No session cookie present on homepage response.")
                    time.sleep(1.5 * attempt)
                    continue

                resp = self._http.get(
                    SESSION_VALIDATE_URL,
                    params={"session_id": raw_cookie},
                    headers=BASE_HEADERS,
                    timeout=12,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    token = data.get("session_id") or data.get("token") or raw_cookie
                    with self._lock:
                        self._token = token
                        self._last_fetch = time.time()
                        self._refresh_count += 1
                        self._last_error = None
                        self._source = "auto"
                    logger.info(f"[Session] Auto-refresh OK - token starts {token[:24]}...")
                    return True

                logger.warning(f"[Session] Validate endpoint returned HTTP {resp.status_code}")

            except requests.exceptions.RequestException as e:
                logger.error(f"[Session] Network error during auto-refresh: {e}")
            except Exception as e:
                logger.error(f"[Session] Unexpected error during auto-refresh: {e}")

            time.sleep(1.5 * attempt)

        self._last_error = self._last_error or "Auto-refresh failed after retries."
        logger.error("[Session] Auto-refresh gave up.")
        return False

    # ── connectivity test (for debugging) ─────────────────────

    def test_connection(self) -> dict:
        """Fire ONE real check request with the current token and report
        exactly what happened - used by the 'Test connection' button so you
        can see whether a token is good before starting a bulk run."""
        if not self.is_valid():
            return {"ok": False, "message": "No session token set."}

        probe_number = "8065551234"  # harmless fake number just to probe the endpoint
        try:
            resp = self._http.get(
                TCPA_CHECK_URL,
                params={"x": probe_number},
                headers=self.get_headers(),
                timeout=REQUEST_TIMEOUT_SECS,
            )
            logger.info(f"[Session] Test connection -> HTTP {resp.status_code}")

            if resp.status_code in (401, 403):
                return {"ok": False, "message": f"HTTP {resp.status_code} - token rejected/expired."}
            if resp.status_code == 429:
                return {"ok": False, "message": "HTTP 429 - rate limited right now, try again shortly."}
            if resp.status_code != 200:
                return {"ok": False, "message": f"Unexpected HTTP {resp.status_code}."}

            try:
                data = resp.json()
            except ValueError:
                return {"ok": False, "message": "HTTP 200 but response wasn't valid JSON - likely a challenge page."}

            return {"ok": True, "message": "Token is valid - API responded normally.", "sample": data}

        except requests.exceptions.RequestException as e:
            logger.error(f"[Session] Test connection network error: {e}")
            return {"ok": False, "message": f"Network error: {e}"}

    # ── accessors ──────────────────────────────────────────────

    def get_headers(self) -> dict:
        h = dict(BASE_HEADERS)
        with self._lock:
            if self._token:
                h["X-Session-ID"] = self._token
        return h

    def is_valid(self) -> bool:
        with self._lock:
            return self._token is not None

    @property
    def source(self) -> str:
        return self._source

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def token_preview(self) -> str:
        with self._lock:
            return (self._token[:32] + "...") if self._token else "None"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _extract_session_cookie(self) -> str | None:
        for cookie in self._http.cookies:
            if "session" in cookie.name.lower():
                return cookie.value
        return None
