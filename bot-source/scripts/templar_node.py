#!/usr/bin/env python3
"""Run the VPN node onboarding CLI from a source checkout."""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.templar_node.cli import main  # noqa: E402


if __name__ == '__main__':
    raise SystemExit(main())
