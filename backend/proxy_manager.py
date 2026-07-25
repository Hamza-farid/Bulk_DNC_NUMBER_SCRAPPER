"""
proxy_manager.py
Optional proxy pool with health tracking. Fully optional - if proxies.txt
is missing or empty, the engine just runs direct (no proxy) without erroring.

Format in proxies.txt, one per line:
    IP:PORT                    (no auth - e.g. IP-whitelisted proxies)
    IP PORT                    (same, space-separated)
    IP:PORT:USERNAME:PASSWORD  (username/password auth - most paid providers)
    IP PORT USERNAME PASSWORD  (same, space-separated)
Lines starting with # are ignored.
"""

import logging
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ProxyStats:
    address: str
    username: str | None = None
    password: str | None = None
    success: int = 0
    failures: int = 0
    banned: bool = False

    @property
    def total(self):
        return self.success + self.failures

    @property
    def success_rate(self):
        return (self.success / self.total * 100) if self.total else 100.0

    @property
    def is_healthy(self):
        if self.banned:
            return False
        if self.total >= 10 and self.success_rate < 40:
            return False
        return True


class ProxyManager:
    """Thread-safe proxy pool. Safe to use with zero proxies loaded."""

    def __init__(self, proxies_file: str | Path):
        self._file = Path(proxies_file)
        self._pool: dict[str, ProxyStats] = {}
        self._lock = threading.Lock()
        self._load()

    def get(self) -> str | None:
        with self._lock:
            healthy = [s for s in self._pool.values() if s.is_healthy]
            if not healthy:
                return None
            return random.choice(healthy).address

    def to_requests_dict(self, proxy: str | None) -> dict | None:
        if not proxy:
            return None
        with self._lock:
            stats = self._pool.get(proxy)
        if stats and stats.username:
            url = f"http://{stats.username}:{stats.password}@{proxy}"
        else:
            url = f"http://{proxy}"
        return {"http": url, "https": url}

    def mark_success(self, proxy: str):
        with self._lock:
            if proxy in self._pool:
                self._pool[proxy].success += 1

    def mark_failure(self, proxy: str, ban: bool = False):
        with self._lock:
            if proxy in self._pool:
                self._pool[proxy].failures += 1
                if ban:
                    self._pool[proxy].banned = True
                    logger.warning(f"[Proxy] Banned: {proxy}")

    def reload(self):
        self._load()

    def remove_dead(self):
        with self._lock:
            before = len(self._pool)
            dead = [addr for addr, s in self._pool.items() if not s.is_healthy]
            for addr in dead:
                del self._pool[addr]
            removed = before - len(self._pool)
        if removed:
            logger.info(f"[Proxy] Removed {removed} dead proxies. Pool now {len(self._pool)}.")

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._pool)

    @property
    def healthy_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._pool.values() if s.is_healthy)

    @property
    def banned_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._pool.values() if s.banned)

    def all_addresses(self) -> list[str]:
        with self._lock:
            return list(self._pool.keys())

    def _load(self):
        parsed: list[tuple[str, str | None, str | None]] = []
        try:
            with open(self._file, encoding="utf-8") as f:
                for lineno, raw_line in enumerate(f, start=1):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue

                    fields = re.split(r"[:\s]+", line)
                    if len(fields) == 2 and fields[1].isdigit():
                        ip, port = fields
                        parsed.append((f"{ip}:{port}", None, None))
                    elif len(fields) == 4 and fields[1].isdigit():
                        ip, port, user, pwd = fields
                        parsed.append((f"{ip}:{port}", user, pwd))
                    else:
                        logger.warning(f"[Proxy] Skipping malformed line {lineno} in {self._file.name}: {line!r}")

            with self._lock:
                for addr, user, pwd in parsed:
                    if addr not in self._pool:
                        self._pool[addr] = ProxyStats(address=addr, username=user, password=pwd)
                    else:
                        # Refresh credentials in case the file changed them on reload.
                        self._pool[addr].username = user
                        self._pool[addr].password = pwd

            if parsed:
                auth_count = sum(1 for _, u, _ in parsed if u)
                logger.info(
                    f"[Proxy] Loaded {len(parsed)} proxies from {self._file.name} "
                    f"({auth_count} with username/password auth)."
                )
            else:
                logger.info(f"[Proxy] '{self._file.name}' empty or missing - running without proxies.")
        except FileNotFoundError:
            logger.info(f"[Proxy] '{self._file.name}' not found - running without proxies.")
