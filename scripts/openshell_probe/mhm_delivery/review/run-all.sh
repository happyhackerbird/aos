#!/bin/sh
# Run the four review rounds from any working directory; values are observations, not assertions.
set -e
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
node review/bridge-probe.mjs large
node review/bridge-probe.mjs small
node review/bridge-probe.mjs stops
node review/a2/bridge-probe.mjs normal
node review/a2/bridge-probe.mjs inbox-write-failure
node review/a2/bridge-probe.mjs stop-write-failure
python3 review/guards-and-pins.py
python3 review/retention-and-wait.py
python3 review/shared-probe.py
python3 review/a2/boundaries.py
node review/a2/ownership.mjs
python3 review/a2/codex-redaction.py
python3 review/a2/replay-contract.py
python3 review/a2/coexistence.py
python3 review/a3/deadline.py
python3 review/a3/lost-ack.py
python3 review/a3/stop-order.py
node review/a3/bridge.mjs
python3 review/a3/version-compat.py
node review/a3/readiness.mjs
python3 review/a3/origin.py
python3 review/a3/positive.py
python3 review/a4/review.py
node review/a4/bridge.mjs
python3 review/a4/origin.py
