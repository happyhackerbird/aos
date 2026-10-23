#!/usr/bin/env python3
"""Summarize an `openshell logs` capture for a probe record: the DENIED lines grouped and counted,
and the allowed destinations counted, so the evidence directory can keep a short gateway-denied.txt
while the full log (hundreds of KB) stays in the session scratchpad.

    uv run python scripts/openshell_probe/gateway_denied.py <gateway-log.txt> > <evidence>/gateway-denied.txt
"""
from __future__ import annotations

import collections
import re
import sys

STAMP = re.compile(r"^\S+\s+\S+\s+")           # the two leading timestamp/level tokens
ALLOWED = re.compile(r"ALLOWED\s+(.*?)(?:\s+\[|$)")


def main(path: str) -> int:
    lines = open(path, errors="replace").read().splitlines()
    denied = collections.Counter()
    allowed = collections.Counter()
    for line in lines:
        body = STAMP.sub("", line).strip()
        if "DENIED" in line:
            denied[body[:220]] += 1
        elif "ALLOWED" in line:
            m = ALLOWED.search(body)
            if m:
                target = m.group(1)
                target = re.sub(r"^\S+\(\d+\)\s*->\s*", "", target)   # drop the "binary(pid) -> " head
                allowed[target[:120]] += 1
    print(f"gateway log lines: {len(lines)}; DENIED lines: {sum(denied.values())}; ALLOWED lines: {sum(allowed.values())}")
    print("\nDENIED (count, line):")
    for text, n in denied.most_common():
        print(f"  {n:4d}  {text}")
    print("\nALLOWED destinations (count, target):")
    for text, n in allowed.most_common(40):
        print(f"  {n:4d}  {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
