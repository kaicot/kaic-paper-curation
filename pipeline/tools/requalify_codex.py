#!/usr/bin/env python3
"""Qualify a signed Codex candidate, or check last-good state without generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.providers.codex_gateway import CodexGateway, CodexGatewayError, JsonObject  # noqa: E402


def _canonical(value: JsonObject) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify or verify the signed Codex CLI saved-auth boundary")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--accept-current-signed-binary", action="store_true")
    mode.add_argument("--verify-only", action="store_true", help="No generation and no qualification-state write")
    parser.add_argument("--role", action="append", help="Role to canary while accepting; repeat as needed")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        gateway = CodexGateway.production(ROOT)
        if args.role and not args.accept_current_signed_binary:
            parser.error("--role requires --accept-current-signed-binary")
        attestation = gateway.requalify(
            accept=args.accept_current_signed_binary,
            roles=args.role,
        )
        raw_routes = attestation.get("qualified_routes")
        result: JsonObject = {
            "attestation_sha256": hashlib.sha256(_canonical(attestation)).hexdigest(),
            "binary_sha256": attestation["binary_sha256"],
            "cache_compatibility_epoch": attestation["cache_compatibility_epoch"],
            "cli_version": attestation["cli_version"],
            "contract_sha256": attestation["contract_sha256"],
            "mode": "accepted" if args.accept_current_signed_binary else "verified",
            "policy_sha256": attestation["policy_sha256"],
            "qualified_route_count": len(raw_routes) if isinstance(raw_routes, dict) else 0,
            "schema": "codex-requalification-result-v2",
            "schema_version": 2,
            "status": "PASS",
        }
    except CodexGatewayError as error:
        result = {"code": error.code, "schema": "codex-requalification-result-v2", "schema_version": 2, "status": "FAIL"}
    encoded = _canonical(result)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_bytes(encoded)
    sys.stdout.buffer.write(encoded)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
