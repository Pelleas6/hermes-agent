#!/usr/bin/env python3
"""Compatibility entrypoint for local Hermes observability.

Implementation lives in the ``hermes_observability`` package so every module
remains small, testable, and directly installable on the VPS.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from hermes_observability import *  # noqa: F401,F403
from hermes_observability.app import main


if __name__ == "__main__":
    raise SystemExit(main())
