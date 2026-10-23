#!/usr/bin/env python3
"""Thin wrapper: the renderer lives in ``agent_os.openshell_policy`` (same file synced beside this
script on probe hosts that have no agent_os checkout)."""
from __future__ import annotations

import os
import sys

try:
    from agent_os.openshell_policy import *  # noqa: F401,F403
    from agent_os.openshell_policy import main
except ImportError:  # probe host: the module is synced next to this script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from openshell_policy import *  # type: ignore  # noqa: F401,F403
    from openshell_policy import main  # type: ignore

if __name__ == "__main__":
    sys.exit(main())
