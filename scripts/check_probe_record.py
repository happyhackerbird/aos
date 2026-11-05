"""Delivery check for a probe record: the agreed headings, a comparison, and a stated verdict.

    python3 scripts/check_probe_record.py <slug>

It does not judge the content; the coordinator does. "Cause: not established" is a valid
verdict, so a worker never has to name a cause to pass this check.
"""
import re
import sys
from pathlib import Path

HEADINGS = ["## Question", "## Comparison", "## Cause", "## Proposed fix", "## Unknowns"]
LOCATIONS = "runner|image|harness|host|OpenShell|upstream|not established"


def fail(message: str) -> None:
    print(f"check_probe_record: {message}", file=sys.stderr)
    sys.exit(1)


if len(sys.argv) != 2:
    fail("usage: check_probe_record.py <slug>")
path = Path("docs/probes") / f"{sys.argv[1]}.md"
if not path.is_file():
    fail(f"missing {path}")
lines = path.read_text(encoding="utf-8").splitlines()

positions = []
for heading in HEADINGS:
    hits = [index for index, line in enumerate(lines) if line.strip() == heading]
    if not hits:
        fail(f"{path} lacks heading '{heading}'")
    positions.append(hits[0])
if positions != sorted(positions):
    fail("headings out of order")

ends = positions[1:] + [len(lines)]
sections = {heading: lines[start + 1:end] for heading, start, end in zip(HEADINGS, positions, ends)}
for heading, body in sections.items():
    if not any(line.strip() for line in body):
        fail(f"'{heading}' is empty")

rows = [line for line in sections["## Comparison"]
        if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")]
if len(rows) < 4:
    fail("the comparison table needs a header and at least three observation rows")

cause = [line for line in sections["## Cause"] if re.fullmatch(r"Cause: \S.*", line.strip())]
if len(cause) != 1:
    fail("'## Cause' needs exactly one line starting 'Cause: '")
location = [line for line in sections["## Cause"] if re.fullmatch(rf"Location: ({LOCATIONS})", line.strip())]
if len(location) != 1:
    fail(f"'## Cause' needs exactly one line 'Location: <{LOCATIONS}>'")

print(f"check_probe_record: {path} delivered: {location[0].strip()}")
