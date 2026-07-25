"""
checker_engine.py
Multi-threaded engine that walks the bulk number list and calls
infolookup.site's /api/tcpa for each one.

Real response shape (confirmed by direct testing against the live API):
    {"status":"ok","phone":"...","listed":"No","type":"No","state":"TX","ndnc":"No","sdnc":"No"}

There is no separate "litigator"/"blacklist" field from this endpoint —
their own frontend (test2.js) derives those from a "results.status_array"
field that this provider never actually populates, so for this data
source those two are effectively always "not flagged". A number counts
as DNC/flagged here if ndnc == Yes, sdnc == Yes, or listed == Yes.

Every meaningful event is logged (chunk starts, token mint/refresh,
retries, error types) so a stalled or failing run has a reason attached
in the logs instead of just going quiet.
"""

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import requests

from backend.config import BASE_HEADERS, MAX_RETRIES_PER_NUMBER, REQUEST_TIMEOUT_SECS, TCPA_CHECK_URL
from backend.csv_store import CSVStore
from backend.proxy_manager import ProxyManager
from backend.session_manager import SessionManager

logger = logging.getLogger(__name__)

VALID = "VALID"
DNC = "DNC"
NON_EXISTENT = "NON_EXISTENT"
TOKEN_REJECTED = "TOKEN_REJECTED"
ERROR = "ERROR"


@dataclass
class RunStats:
    total_queued: int = 0
    processed: int = 0
    valid: int = 0
    dnc: int = 0
    non_existent: int = 0
    errors: int = 0
    token_refreshes: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    running: bool = False
    paused: bool = False
    stop_requested: bool = False
    current_chunk: int = 0
    total_chunks: int = 0
    last_numbers: list = field(default_factory=list)
    fatal_error: str | None = None

    @property
    def elapsed(self) -> float:
        return (self.end_time or time.time()) - self.start_time

    @property
    def rate_per_sec(self) -> float:
        return self.processed / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float:
        remaining = self.total_queued - self.processed
        return remaining / self.rate_per_sec if self.rate_per_sec > 0 else 0.0

    @property
    def progress_pct(self) -> float:
        return (self.processed / self.total_queued * 100) if self.total_queued else 0.0


class CheckerEngine:
    def __init__(
        self,
        session_mgr: SessionManager,
        proxy_mgr: ProxyManager,
        csv_store: CSVStore,
        max_threads: int = 40,
        chunk_size: int = 2000,
        sleep_between_chunks: float = 2.0,
        on_result: Callable | None = None,
    ):
        self.session = session_mgr
        self.proxies = proxy_mgr
        self.csv = csv_store
        self.threads = max_threads
        self.chunk_sz = chunk_size
        self.sleep_bw = sleep_between_chunks
        self.on_result = on_result

        self.stats = RunStats()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # The site tolerates ~1 in-flight request per token/IP at a time —
        # confirmed by testing: 2+ simultaneous requests on the same token
        # get rejected with 403 "Access denied" most of the time, even
        # though the token itself is valid. So real parallelism only comes
        # from using multiple proxies (each is its own IP + token); within
        # one connection (proxy or "direct"), requests must be serialized.
        # This lock enforces that per connection-key regardless of how many
        # worker threads are configured.
        self._call_locks: dict[str, threading.Lock] = {}
        self._call_locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._call_locks_guard:
            if key not in self._call_locks:
                self._call_locks[key] = threading.Lock()
            return self._call_locks[key]

    def stop(self):
        self.stats.stop_requested = True
        self._pause_event.set()
        logger.info("[Engine] Stop requested.")

    def pause(self):
        self.stats.paused = True
        self._pause_event.clear()
        logger.info("[Engine] Paused.")

    def resume(self):
        self.stats.paused = False
        self._pause_event.set()
        logger.info("[Engine] Resumed.")

    def run(self, numbers: list[str]):
        self.stats = RunStats()
        self.stats.running = True
        self.stats.total_queued = len(numbers)

        logger.info(f"[Engine] Starting run — {len(numbers)} numbers queued, {self.threads} threads.")

        # Prime the direct-connection token up front so failures show early.
        session, token = self.session.get_session_and_token(None, None)
        if not token:
            msg = self.session.last_error or "Could not obtain a lookup token."
            logger.error(f"[Engine] Cannot start: {msg}")
            self.stats.fatal_error = msg
            self.stats.running = False
            return

        chunks = [numbers[i:i + self.chunk_sz] for i in range(0, len(numbers), self.chunk_sz)]
        self.stats.total_chunks = len(chunks)

        for idx, chunk in enumerate(chunks):
            if self.stats.stop_requested:
                logger.info("[Engine] Stop flag seen — halting before next chunk.")
                break

            self.stats.current_chunk = idx + 1
            logger.info(f"[Engine] Chunk {idx + 1}/{len(chunks)} — {len(chunk)} numbers.")

            self._process_chunk(chunk)
            self.proxies.remove_dead()

            if idx < len(chunks) - 1 and not self.stats.stop_requested:
                logger.info(f"[Engine] Sleeping {self.sleep_bw}s between chunks.")
                time.sleep(self.sleep_bw)

        self.csv.flush_all()
        self.stats.running = False
        self.stats.end_time = time.time()
        logger.info(
            f"[Engine] Run finished. processed={self.stats.processed} "
            f"valid={self.stats.valid} dnc={self.stats.dnc} "
            f"non_existent={self.stats.non_existent} errors={self.stats.errors} "
            f"in {self.stats.elapsed:.1f}s."
        )

    def _process_chunk(self, chunk: list[str]):
        executor = ThreadPoolExecutor(max_workers=self.threads)
        futures = {executor.submit(self._check_one, n): n for n in chunk}

        try:
            for future in as_completed(futures):
                self._pause_event.wait()

                if self.stats.stop_requested:
                    break

                number = futures[future]
                try:
                    status, data = future.result()
                except Exception as e:
                    logger.debug(f"[Engine] Unhandled future exception for {number}: {e}")
                    self.stats.errors += 1
                    continue

                self._handle_result(status, data)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _check_one(self, number: str):
        proxy = self.proxies.get()
        proxies_dict = self.proxies.to_requests_dict(proxy)
        key = proxy or "direct"

        with self._lock_for(key):
            return self._check_one_locked(number, proxy, proxies_dict)

    def _check_one_locked(self, number: str, proxy: str | None, proxies_dict: dict | None):
        for attempt in range(1, MAX_RETRIES_PER_NUMBER + 1):
            session, token = self.session.get_session_and_token(proxy, proxies_dict)
            if not token:
                logger.warning(f"[Engine] {number} -> no lookup token available for proxy={proxy!r}.")
                return ERROR, number

            try:
                resp = session.get(
                    TCPA_CHECK_URL,
                    params={"x": number, "_t": token, "pi": "1"},
                    headers=BASE_HEADERS,
                    proxies=proxies_dict,
                    timeout=REQUEST_TIMEOUT_SECS,
                )

                if resp.status_code in (401, 403):
                    logger.warning(f"[Engine] {number} -> HTTP {resp.status_code}, token rejected (proxy={proxy!r}). Re-minting.")
                    self.session.invalidate(proxy)
                    if attempt == MAX_RETRIES_PER_NUMBER:
                        return TOKEN_REJECTED, number
                    continue

                if resp.status_code == 429:
                    if proxy:
                        self.proxies.mark_failure(proxy, ban=False)
                    wait = min(2 ** attempt + random.uniform(0, 1), 20)
                    logger.warning(f"[Engine] {number} -> HTTP 429 rate limited, backing off {wait:.1f}s (attempt {attempt}).")
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    logger.debug(f"[Engine] {number} -> unexpected HTTP {resp.status_code}.")
                    if proxy:
                        self.proxies.mark_failure(proxy)
                    return ERROR, number

                try:
                    data = resp.json()
                except ValueError:
                    logger.debug(f"[Engine] {number} -> HTTP 200 but non-JSON body.")
                    if proxy:
                        self.proxies.mark_failure(proxy)
                    return ERROR, number

                if proxy:
                    self.proxies.mark_success(proxy)

                if data.get("status") != "ok":
                    return NON_EXISTENT, number

                payload = {
                    "phone": number,
                    "state": data.get("state", "Unknown"),
                    "type": str(data.get("type", "Unknown")),
                    "listed": str(data.get("listed", "no")).strip().lower(),
                    "ndnc": str(data.get("ndnc", "no")).strip().lower(),
                    "sdnc": str(data.get("sdnc", "no")).strip().lower(),
                }
                flagged = payload["ndnc"] == "yes" or payload["sdnc"] == "yes" or payload["listed"] == "yes"
                return (DNC if flagged else VALID), payload

            except requests.exceptions.Timeout:
                logger.debug(f"[Engine] {number} -> timeout (attempt {attempt}).")
                if proxy:
                    self.proxies.mark_failure(proxy)
                if attempt == MAX_RETRIES_PER_NUMBER:
                    return ERROR, number
            except requests.exceptions.RequestException as e:
                logger.debug(f"[Engine] {number} -> network error: {e} (attempt {attempt}).")
                if proxy:
                    self.proxies.mark_failure(proxy)
                if attempt == MAX_RETRIES_PER_NUMBER:
                    return ERROR, number
            except Exception as e:
                logger.warning(f"[Engine] {number} -> unexpected exception: {e}")
                if proxy:
                    self.proxies.mark_failure(proxy)
                return ERROR, number

        return ERROR, number

    def _handle_result(self, status: str, data):
        self.stats.processed += 1

        if status == TOKEN_REJECTED:
            self.stats.token_refreshes += 1
            logger.warning(f"[Engine] Token kept getting rejected for {data} even after re-mint.")
            self.stats.errors += 1
            return

        if status == VALID:
            self.stats.valid += 1
            self.csv.add_valid(data)
            self._push_last({"status": "Valid", "phone": data["phone"], "state": data["state"], "type": data["type"]})
        elif status == DNC:
            self.stats.dnc += 1
            self.csv.add_dnc(data)
            self._push_last({"status": "DNC", "phone": data["phone"], "state": data["state"], "type": data["type"]})
        elif status == NON_EXISTENT:
            self.stats.non_existent += 1
            self.csv.add_non_existent(data)
            self._push_last({"status": "Not found", "phone": data, "state": "-", "type": "-"})
        else:
            self.stats.errors += 1

        if self.on_result:
            try:
                self.on_result(status, data)
            except Exception:
                logger.debug("[Engine] on_result callback raised — ignored.", exc_info=True)

    def _push_last(self, entry: dict):
        self.stats.last_numbers.insert(0, entry)
        self.stats.last_numbers = self.stats.last_numbers[:12]
