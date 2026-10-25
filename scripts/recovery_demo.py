#!/usr/bin/env python3
"""Run the recovery demo from a fresh clone without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_harness.demo import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
