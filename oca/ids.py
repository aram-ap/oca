"""Human-readable identifiers: INC-0042, DEP-0081, SBX-0003."""

import re

_ID = re.compile(r"^([A-Z]{3})-(\d{4,})$")


def format_id(prefix: str, n: int) -> str:
    if n < 1:
        raise ValueError("Identifier counters start at 1")
    return f"{prefix}-{n:04d}"


def parse_id(value: str) -> tuple[str, int]:
    match = _ID.match(value)
    if not match:
        raise ValueError(f"Malformed identifier: {value!r}")
    return match.group(1), int(match.group(2))
