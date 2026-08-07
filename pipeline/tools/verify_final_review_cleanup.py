#!/usr/bin/env python3
"""Todo 23 — final-review cleanup verification.

Compares final-review-state-v1 before/after captures for F1 (protected
evidence identity/hash equality) and F2 (registered processes exited,
listeners absent, registered temp paths absent). Phase/timestamp fields are
excluded from comparison.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXCLUDED_KEYS = {"captured_at_utc", "final_head"}


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verifier", required=True, choices=["F1", "F2", "F3", "F4"])
    parser.add_argument("--kind", required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    before = load_state(args.before)
    after = load_state(args.after)
    failures: list[str] = []

    if args.verifier == "F1":
        before_protected = {e["path"]: e for e in before.get("protected_evidence", [])}
        after_protected = {e["path"]: e for e in after.get("protected_evidence", [])}
        if set(before_protected) != set(after_protected):
            failures.append("protected evidence path set changed")
        for path, entry in before_protected.items():
            after_entry = after_protected.get(path)
            if after_entry is None:
                failures.append(f"protected evidence missing after: {path}")
                continue
            for key in ("file_id", "size", "sha256"):
                if entry.get(key) != after_entry.get(key):
                    failures.append(f"protected evidence {key} drift: {path}")
    elif args.verifier == "F2":
        registered_pids = {p["pid"] for p in before.get("registered_processes", [])}
        for process in after.get("registered_processes", []):
            if process.get("pid") in registered_pids:
                failures.append(f"registered process still present: {process.get('pid')}")
        before_listeners = {(l.get("pid"), l.get("address"), l.get("port")) for l in before.get("listeners", [])}
        for listener in after.get("listeners", []):
            if (listener.get("pid"), listener.get("address"), listener.get("port")) in before_listeners:
                failures.append("registered listener still present")
        before_temps = {t["path"] for t in before.get("registered_temp_paths", [])}
        for temp in after.get("registered_temp_paths", []):
            if temp.get("path") in before_temps and temp.get("exists") is True:
                failures.append(f"registered temp path still exists: {temp.get('path')}")
    else:
        failures.append(f"unsupported verifier {args.verifier}")

    payload = {
        "schema": "final-review-cleanup-v1",
        "schema_version": 1,
        "verifier": args.verifier,
        "kind": args.kind,
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    sys.stdout.write(text + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
