"""
number_builder.py
Builds the bulk list of phone numbers to check using the standard NANP
(North American Numbering Plan) split that the client wants to pick from:

    [ NPA  ] [ NXX ]  [ Line ]
     3 digits 3 digits  4 digits
     area code exchange line-number

e.g. area code "806", exchange range 549–560, line range 0000–9999
     -> 806-549-0000, 806-549-0001, ... 806-560-9999

This replaces the old single flat "start_suffix..end_suffix" 7-digit
range with three independently-chosen ranges, exactly like the client asked.
"""

import logging
import random

logger = logging.getLogger(__name__)

MAX_EXCHANGE = 999   # NXX segment: 000-999
MAX_LINE = 9999      # line segment: 0000-9999


def build_number_list(
    area_code: str,
    exch_start: int,
    exch_end: int,
    line_start: int,
    line_end: int,
    already_done: set | None = None,
    shuffle: bool = True,
) -> list[str]:
    """Generate every 10-digit number across the exchange x line grid."""
    already_done = already_done or set()
    numbers = []

    for exch in range(exch_start, exch_end + 1):
        exch_str = str(exch).zfill(3)
        for line in range(line_start, line_end + 1):
            num = f"{area_code}{exch_str}{str(line).zfill(4)}"
            if num not in already_done:
                numbers.append(num)

    if shuffle:
        random.shuffle(numbers)

    total_possible = (exch_end - exch_start + 1) * (line_end - line_start + 1)
    skipped = total_possible - len(numbers)
    logger.info(
        f"[Builder] {area_code} exch {exch_start:03d}-{exch_end:03d} x "
        f"line {line_start:04d}-{line_end:04d} -> {len(numbers)} to check, "
        f"{skipped} skipped (already done)"
    )
    return numbers


def range_summary(
    area_code: str,
    exch_start: int,
    exch_end: int,
    line_start: int,
    line_end: int,
) -> dict:
    """Summary dict used to preview the range in the UI before running."""
    total = (exch_end - exch_start + 1) * (line_end - line_start + 1)
    start_num = f"{area_code}{str(exch_start).zfill(3)}{str(line_start).zfill(4)}"
    end_num = f"{area_code}{str(exch_end).zfill(3)}{str(line_end).zfill(4)}"
    return {
        "area_code": area_code,
        "start": start_num,
        "end": end_num,
        "total": total,
    }
