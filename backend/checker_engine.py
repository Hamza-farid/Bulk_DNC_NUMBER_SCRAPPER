"""
checker_engine.py
Multi-threaded engine that walks the bulk number list, calls the TCPA API
for each one, and buckets results into valid / dnc / non_existent.

Every meaningful event is logged (chunk starts, session refreshes, retries,
error types) specifically so that when "it just doesn't work" you have a
log line telling you why, instead of a silent stall.
"""

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import requests

from backend.config import MAX_RETRIES_PER_NUMBER, REQUEST_TIMEOUT_SECS, TCPA_CHECK_URL
from backend.csv_store import CSVStore
from backend.proxy_manager import ProxyManager
from backend.session_manager import SessionManager

logger = logging.getLogger(__name__)

VALID = "VALID"
DNC = "DNC"
NON_EXISTENT = "NON_EXISTENT"
SESSION_EXPIRED = "SESSION_EXPIRED"
ERROR = "ERROR"


@dataclass
class RunStats:
    total_queued: int = 0
    processed: int = 0
    valid: int = 0
    dnc: int = 0
    non_existent: int = 0
    errors: int = 0
    session_refreshes: int = 0
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
        max_threads: int = 50,
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
        self._http = requests.Session()
        self._pause_event = threading.Event()
        self._pause_event.set()

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

        logger.info(f"[Engine] Starting run - {len(numbers)} numbers queued, {self.threads} threads.")

        if not self.session.is_valid():
            logger.info("[Engine] No session token present - attempting auto-refresh before starting.")
            if not self.session.refresh():
                msg = self.session.last_error or "No session token available."
                logger.error(f"[Engine] Cannot start: {msg}")
                self.stats.fatal_error = msg
                self.stats.running = False
                return

        chunks = [numbers[i:i + self.chunk_sz] for i in range(0, len(numbers), self.chunk_sz)]
        self.stats.total_chunks = len(chunks)

        for idx, chunk in enumerate(chunks):
            if self.stats.stop_requested:
                logger.info("[Engine] Stop flag seen - halting before next chunk.")
                break

            self.stats.current_chunk = idx + 1
            logger.info(f"[Engine] Chunk {idx + 1}/{len(chunks)} - {len(chunk)} numbers.")

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
        proxies = self.proxies.to_requests_dict(proxy)
        headers = self.session.get_headers()

        for attempt in range(1, MAX_RETRIES_PER_NUMBER + 1):
            try:
                resp = self._http.get(
                    TCPA_CHECK_URL,
                    params={"x": number},
                    headers=headers,
                    proxies=proxies,
                    timeout=REQUEST_TIMEOUT_SECS,
                )

                if resp.status_code in (401, 403):
                    logger.warning(f"[Engine] {number} -> HTTP {resp.status_code} (session likely expired).")
                    return SESSION_EXPIRED, number

                if resp.status_code == 429:
                    if proxy:
                        self.proxies.mark_failure(proxy, ban=False)
                    wait = min(2 ** attempt + random.uniform(0, 1), 20)
                    logger.warning(f"[Engine] {number} -> HTTP 429 rate limited, backing off {wait:.1f}s (attempt {attempt}).")
                    time.sleep(wait)
                    proxy = self.proxies.get()
                    proxies = self.proxies.to_requests_dict(proxy)
                    headers = self.session.get_headers()
                    continue

                if resp.status_code != 200:
                    logger.debug(f"[Engine] {number} -> unexpected HTTP {resp.status_code}.")
                    if proxy:
                        self.proxies.mark_failure(proxy)
                    return ERROR, number

                try:
                    data = resp.json()
                except ValueError:
                    logger.debug(f"[Engine] {number} -> HTTP 200 but non-JSON body (likely a challenge page).")
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
                    "type": data.get("type", "Unknown"),
                    "listed": data.get("listed", "Unknown"),
                    "ndnc": str(data.get("ndnc", "no")).strip().lower(),
                    "sdnc": str(data.get("sdnc", "no")).strip().lower(),
                    "litigator": str(data.get("litigator", "no")).strip().lower(),
                    "blacklist": str(data.get("blacklist", "no")).strip().lower(),
                }
                flagged = any(payload[k] == "yes" for k in ("ndnc", "sdnc", "litigator", "blacklist"))
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

        if status == SESSION_EXPIRED:
            self.stats.session_refreshes += 1
            logger.warning("[Engine] Session expired mid-run - attempting auto-refresh...")
            ok = self.session.refresh(max_retries=2)
            if not ok:
                msg = (
                    self.session.last_error
                    or "Session refresh failed mid-run. Paste a fresh token and resume."
                )
                logger.error(f"[Engine] {msg} Stopping run.")
                self.stats.fatal_error = msg
                self.stats.stop_requested = True
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
                logger.debug("[Engine] on_result callback raised - ignored.", exc_info=True)

    def _push_last(self, entry: dict):
        self.stats.last_numbers.insert(0, entry)
        self.stats.last_numbers = self.stats.last_numbers[:12]
