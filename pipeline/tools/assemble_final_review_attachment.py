#!/usr/bin/env python3
"""Todo 23 — final-review attachment assembler.

Aggregates command envelopes (+ optional control results) into a canonical
nonempty attachment manifest. Accepts only the frozen producer mapping from
final-review-requirements-v1.json; invented filler is rejected. A quiet
exit-0 command still yields a nonempty manifest because each envelope is
nonempty by construction.
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
    parser.add_argument("--verifier", required=True, choices=["F1", "F2", "F3", "F4"])
    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--envelope", type=Path, action="append", required=True)
    parser.add_argument("--control", type=Path, action="append")
    args = parser.parse_args()

    requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    verifier = requirements["verifiers"].get(args.verifier)
    if verifier is None:
        sys.stderr.write(f"unknown verifier {args.verifier}\n")
        return 2
    if args.attachment_id not in verifier["attachments"]:
        sys.stderr.write(f"unknown attachment id {args.attachment_id!r} for {args.verifier}\n")
        return 2

    envelopes: list[dict] = []
    for path in args.envelope:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        command_id = envelope.get("command_id")
        producers = verifier["producers"].get(command_id, [])
        if args.attachment_id not in producers:
            sys.stderr.write(
                f"filler rejected: {command_id!r} does not produce {args.attachment_id!r} for {args.verifier}\n"
            )
            return 2
        envelopes.append(envelope)

    controls: list[dict] = []
    for path in args.control or []:
        controls.append(json.loads(path.read_text(encoding="utf-8")))

    constituents: list[dict] = []
    for envelope in envelopes:
        constituents.append(
            {
                "kind": "envelope",
                "command_id": envelope["command_id"],
                "sha256": hashlib.sha256(
                    json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        )
    for control in controls:
        constituents.append(
            {
                "kind": "control",
                "sha256": hashlib.sha256(
                    json.dumps(control, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        )

    manifest = {
        "schema": "final-review-attachment-v1",
        "schema_version": 1,
        "verifier": args.verifier,
        "attachment_id": args.attachment_id,
        "envelope_count": len(envelopes),
        "control_count": len(controls),
        "constituents": constituents,
    }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(payload + "\n")
    sys.stdout.write(payload + "\n")
    return 0 if constituents else 1


if __name__ == "__main__":
    raise SystemExit(main())
