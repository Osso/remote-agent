#!/usr/bin/env python3
"""Run project unittest discovery; optionally select unit test filenames."""

import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent
patterns = sys.argv[1:]
if patterns[:1] == ["unit"]:
    patterns = patterns[1:]
suite = unittest.TestSuite()
for pattern in patterns or ["test_*.py"]:
    suite.addTests(
        unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern)
    )
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
