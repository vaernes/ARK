#!/usr/bin/env python3
"""Run the unit suite and reject missing-dependency skips in CI."""

import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent.parent
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print(f"ERROR: {len(result.skipped)} unit tests were skipped.", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0 if result.wasSuccessful() else 1)
