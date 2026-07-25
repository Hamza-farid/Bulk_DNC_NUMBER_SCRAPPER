"""
csv_store.py
Thread-safe, buffered CSV writer for the three result categories, with
resume support (numbers already in the CSVs are skipped on the next run).
"""

import csv
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDS_FULL = ["phone", "state", "type", "listed", "ndnc", "sdnc", "litigator", "blacklist"]
FIELDS_NON = ["phone"]


class CSVStore:
    def __init__(self, data_dir: str | Path):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self.valid_path = self._dir / "valid_numbers.csv"
        self.dnc_path = self._dir / "dnc_numbers.csv"
        self.noexist_path = self._dir / "non_existent.csv"

        self._valid_buf: list[dict] = []
        self._dnc_buf: list[dict] = []
        self._noexist_buf: list[dict] = []

        self._init_files()

    def load_processed(self) -> set:
        done = set()
        for path in (self.valid_path, self.dnc_path, self.noexist_path):
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("phone"):
                            done.add(row["phone"].strip())
            except FileNotFoundError:
                pass
        logger.info(f"[CSV] Resume: {len(done)} numbers already processed previously.")
        return done

    def add_valid(self, row: dict, flush_every: int = 20):
        with self._lock:
            self._valid_buf.append(row)
            if len(self._valid_buf) >= flush_every:
                self._flush(self.valid_path, FIELDS_FULL, self._valid_buf)
                self._valid_buf.clear()

    def add_dnc(self, row: dict, flush_every: int = 20):
        with self._lock:
            self._dnc_buf.append(row)
            if len(self._dnc_buf) >= flush_every:
                self._flush(self.dnc_path, FIELDS_FULL, self._dnc_buf)
                self._dnc_buf.clear()

    def add_non_existent(self, phone: str, flush_every: int = 100):
        with self._lock:
            self._noexist_buf.append({"phone": phone})
            if len(self._noexist_buf) >= flush_every:
                self._flush(self.noexist_path, FIELDS_NON, self._noexist_buf)
                self._noexist_buf.clear()

    def flush_all(self):
        with self._lock:
            if self._valid_buf:
                self._flush(self.valid_path, FIELDS_FULL, self._valid_buf)
                self._valid_buf.clear()
            if self._dnc_buf:
                self._flush(self.dnc_path, FIELDS_FULL, self._dnc_buf)
                self._dnc_buf.clear()
            if self._noexist_buf:
                self._flush(self.noexist_path, FIELDS_NON, self._noexist_buf)
                self._noexist_buf.clear()
        logger.info("[CSV] Buffers flushed to disk.")

    def counts(self) -> dict:
        def _count(path):
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    return max(0, sum(1 for _ in csv.DictReader(f)))
            except FileNotFoundError:
                return 0

        return {
            "valid": _count(self.valid_path),
            "dnc": _count(self.dnc_path),
            "non_existent": _count(self.noexist_path),
        }

    def read_rows(self, category: str = "all") -> list[dict]:
        if category == "valid":
            return self._read_csv(self.valid_path)
        if category == "dnc":
            return self._read_csv(self.dnc_path)
        if category == "non_existent":
            return self._read_csv(self.noexist_path)
        rows = []
        rows.extend(self._read_csv(self.valid_path))
        rows.extend(self._read_csv(self.dnc_path))
        rows.extend(self._read_csv(self.noexist_path))
        return rows

    def reset(self):
        with self._lock:
            for path in (self.valid_path, self.dnc_path, self.noexist_path):
                path.unlink(missing_ok=True)
            self._valid_buf.clear()
            self._dnc_buf.clear()
            self._noexist_buf.clear()
        self._init_files()
        logger.info("[CSV] All output files reset.")

    def _init_files(self):
        for path, fields in (
            (self.valid_path, FIELDS_FULL),
            (self.dnc_path, FIELDS_FULL),
            (self.noexist_path, FIELDS_NON),
        ):
            if not path.exists():
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=fields).writeheader()

    def _flush(self, path: Path, fields: list, rows: list):
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writerows(rows)

    def _read_csv(self, path: Path) -> list[dict]:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            return []
