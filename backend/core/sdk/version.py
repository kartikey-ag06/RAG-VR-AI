"""SDK version and the version-range check used when a pack is loaded (ADR 0003).

A range is a comma-separated list of conditions, all of which must hold:  ">=1.0,<2.0"   "==1.2.0"   ">=1.1"
Versions are MAJOR.MINOR.PATCH numbers; missing parts count as 0 ("1.0" == "1.0.0").
"""
import re

SDK_VERSION = "1.0.0"          # draft contract; becomes the real 1.0.0 when Phase 5 wires it in

_VERSION = re.compile(r"^\d+(\.\d+){0,2}$")
_COND = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,2})$")


def parse_version(v: str) -> tuple:
    v = str(v).strip()
    if not _VERSION.match(v):
        raise ValueError(f"not a version (expected MAJOR.MINOR.PATCH): {v!r}")
    parts = [int(x) for x in v.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def parse_range(spec: str) -> list:
    conds = []
    for raw in str(spec).split(","):
        m = _COND.match(raw.strip())
        if not m:
            raise ValueError(f"bad version condition {raw.strip()!r} in {spec!r} (use e.g. '>=1.0,<2.0')")
        conds.append((m.group(1), parse_version(m.group(2))))
    if not conds:
        raise ValueError("empty version range")
    return conds


_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def satisfies(version: str, spec: str) -> bool:
    """True if `version` is inside the range `spec`, e.g. satisfies("1.4.0", ">=1.0,<2.0") -> True."""
    v = parse_version(version)
    return all(_OPS[op](v, target) for op, target in parse_range(spec))
