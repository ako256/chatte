#!/usr/bin/env python3
"""Entry point for running Chatte (earth.dlc.Chatte) directly from the repository."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from chatte.app import main

if __name__ == "__main__":
    sys.exit(main())

