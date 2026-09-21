"""Partial-score verifier: fraction of hidden budget tests passing.

Writes {"score", "max_score", "metrics"} to $HARNESSLAB_SCORE_FILE (or stdout).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))

loader = unittest.TestLoader()
try:
    suite = loader.loadTestsFromName("test_hidden_budgets")
except Exception as exc:  # module import failure counts as zero
    result = {"score": 0.0, "max_score": 1.0, "metrics": {"tests_passed": 0, "tests_total": 0, "error": str(exc)}}
else:
    runner = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0)
    outcome = runner.run(suite)
    total = outcome.testsRun
    failed = len(outcome.failures) + len(outcome.errors)
    passed = total - failed
    result = {
        "score": (passed / total) if total else 0.0,
        "max_score": 1.0,
        "metrics": {"tests_passed": passed, "tests_total": total},
    }

target = os.environ.get("HARNESSLAB_SCORE_FILE")
payload = json.dumps(result)
if target:
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(payload)
print(payload)
