"""Run every test module.  python3 -m tests"""

import sys

sys.path.insert(0, ".")

from tests.harness import run_all

sys.exit(run_all())
