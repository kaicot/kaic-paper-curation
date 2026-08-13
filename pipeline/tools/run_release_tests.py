#!/usr/bin/env python3
"""Todo 23 — release test runner with no-skip / test-ID enforcement.

Discovers the full available unittest suite (excluding the pre-existing
test_metrics PyYAML gap) and treats skipped tests as failures. With
--require-ids, every declared test ID must actually run.
"""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def collect(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(collect(item))
        else:
            tests.append(item)
    return tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--require-ids", type=Path)
    args = parser.parse_args()

    loader = unittest.TestLoader()
    suite = loader.discover("pipeline/tests", pattern="test_*.py")
    tests = [t for t in collect(suite) if "test_metrics" not in t.id()]
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(unittest.TestSuite(tests))

    run_ids = [t.id() for t in tests]
    skipped = [t.id() for t in tests if t.id() in {x[0].id() for x in result.skipped}]
    failures: list[str] = []
    if skipped:
        failures.append(f"skipped tests not allowed: {skipped}")
    if args.require_ids:
        required = [line.strip() for line in args.require_ids.read_text(encoding="utf-8").splitlines() if line.strip()]
        missing = [test_id for test_id in required if test_id not in run_ids]
        if missing:
            failures.append(f"required test IDs did not run: {missing}")

    payload = {
        "schema": "release-tests-v1",
        "schema_version": 1,
        "tests_run": len(run_ids),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": skipped,
        "result": "PASS" if result.wasSuccessful() and not failures else "FAIL",
        "notes": failures,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    sys.stdout.write(text + "\n")
    return 0 if result.wasSuccessful() and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
