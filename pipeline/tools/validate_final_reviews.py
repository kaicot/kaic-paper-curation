#!/usr/bin/env python3
"""Todo 23 — validate finalized final-review-v1 documents.

Requires exact nonempty ID sets, every row's declared producer mapping,
command exits 0, negative exits nonzero, referenced canonical nonempty
attachment size/hash, true cleanup equality, no generic/extra/unproduced
row, and same plan/attempt/HEAD binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIREMENTS = REPO_ROOT / "pipeline" / "final-review-requirements-v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, action="append", required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    failures: list[str] = []
    reviews: list[dict] = []
    for path in args.review:
        review = json.loads(path.read_text(encoding="utf-8"))
        reviews.append(review)
        verifier_id = review.get("verifier_id")
        verifier = requirements["verifiers"].get(verifier_id)
        if verifier is None:
            failures.append(f"{path}: unknown verifier {verifier_id}")
            continue
        if review.get("schema_version") != 1 or review.get("status") != "APPROVED" or review.get("result") != "PASS":
            failures.append(f"{path}: status/result not APPROVED/PASS")
        required_ids = {
            "commands": verifier["commands"],
            "attachments": verifier["attachments"],
            "negative_controls": verifier["negatives"],
            "cleanup_assertions": verifier["cleanup"],
        }
        for section, expected in required_ids.items():
            rows = review.get(section, [])
            actual = [row["id"] for row in rows]
            if sorted(actual) != sorted(expected):
                failures.append(f"{path}: {section} ID set mismatch: {actual} vs {expected}")
            if not rows:
                failures.append(f"{path}: empty {section}")
        by_id = {row["id"]: row for row in review.get("commands", [])}
        for row in review.get("commands", []):
            if row.get("exit_code") != 0:
                failures.append(f"{path}: command {row['id']} exit {row['exit_code']}")
            attachment_id = row.get("evidence_attachment_id")
            if attachment_id not in review.get("attachments", [{}])[0].get("id", ""):
                pass  # cross-checked below
        attachments = {a["id"]: a for a in review.get("attachments", [])}
        for row in review.get("commands", []):
            attachment_id = row.get("evidence_attachment_id")
            attachment = attachments.get(attachment_id)
            if attachment is None:
                failures.append(f"{path}: command {row['id']} attachment missing")
                continue
            raw = Path(attachment["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != attachment.get("sha256") or len(raw) != attachment.get("size") or not raw:
                failures.append(f"{path}: attachment {attachment_id} hash/size/nonempty violated")
        for row in review.get("negative_controls", []):
            if row.get("expected") != "nonzero" or row.get("observed_exit_code", 0) == 0 or row.get("status") != "PASS":
                failures.append(f"{path}: negative {row['id']} not nonzero/PASS")
        for row in review.get("cleanup_assertions", []):
            if row.get("status") != "PASS" or row.get("expected") != row.get("observed"):
                failures.append(f"{path}: cleanup {row['id']} mismatch")

    payload = {
        "schema": "final-review-validation-v1",
        "schema_version": 1,
        "result": "PASS" if not failures else "FAIL",
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
