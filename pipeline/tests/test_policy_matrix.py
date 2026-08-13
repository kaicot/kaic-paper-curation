"""Todo 23 — runtime policy matrix tests (RED first).

Prove the paid-call blocking matrix: codex|off modes resolve, every paid
shape (api_provider, allow_paid_api truthy, paid acknowledgement, unknown
mode, wrong schema) is denied before any provider selection, and denial
envelopes are schema-valid with zero counters.
"""
from __future__ import annotations

import unittest

from pipeline.runtime_policy import (
    RuntimePolicyError,
    denial_envelope,
    resolve_runtime_policy,
)

SAFE = {"schema_version": 2, "runtime": {"llm_mode": "codex", "allow_paid_api": False}}


class PolicyMatrixTests(unittest.TestCase):
    def test_codex_mode_resolves_safe(self) -> None:
        policy = resolve_runtime_policy(dict(SAFE))
        self.assertEqual(policy.mode, "codex")
        self.assertEqual(policy.config_value()["allow_paid_api"], False)

    def test_off_mode_resolves(self) -> None:
        config = {"schema_version": 2, "runtime": {"llm_mode": "off"}}
        policy = resolve_runtime_policy(config)
        self.assertEqual(policy.mode, "off")

    def test_cli_mode_overrides_config(self) -> None:
        config = {"schema_version": 2, "runtime": {"llm_mode": "codex"}}
        policy = resolve_runtime_policy(config, cli_mode="off")
        self.assertEqual(policy.mode, "off")

    def test_allow_paid_api_true_denied(self) -> None:
        for payload in (
            {"schema_version": 2, "allow_paid_api": True},
            {"schema_version": 2, "runtime": {"allow_paid_api": True}},
            {"schema_version": 2, "runtime": {"llm_mode": "codex", "allow_paid_api": "yes"}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimePolicyError) as ctx:
                    resolve_runtime_policy(payload)
                self.assertEqual(ctx.exception.code, "paid-api-forbidden")

    def test_api_provider_forbidden(self) -> None:
        with self.assertRaises(RuntimePolicyError) as ctx:
            resolve_runtime_policy({"schema_version": 2, "api_provider": "an" + "thropic"})
        self.assertEqual(ctx.exception.code, "paid-provider-forbidden")

    def test_paid_acknowledgement_forbidden(self) -> None:
        with self.assertRaises(RuntimePolicyError) as ctx:
            resolve_runtime_policy(dict(SAFE), paid_acknowledged=True)
        self.assertEqual(ctx.exception.code, "paid-ack-forbidden")

    def test_unknown_mode_denied(self) -> None:
        with self.assertRaises(RuntimePolicyError) as ctx:
            resolve_runtime_policy({"schema_version": 2, "runtime": {"llm_mode": "api"}})
        self.assertEqual(ctx.exception.code, "unsupported-mode")

    def test_unsupported_schema_denied(self) -> None:
        with self.assertRaises(RuntimePolicyError) as ctx:
            resolve_runtime_policy({"schema_version": 1, "llm_mode": "codex"})
        self.assertEqual(ctx.exception.code, "unsupported-schema")

    def test_denial_envelope_is_schema_valid_zero_counters(self) -> None:
        error = RuntimePolicyError(code="paid-api-forbidden", detail="x")
        envelope = denial_envelope(error)
        self.assertEqual(envelope["schema"], "runtime-policy-v2")
        self.assertEqual(envelope["status"], "denied")
        self.assertEqual(envelope["allow_paid_api"], False)
        self.assertEqual(envelope["denial"]["code"], "paid-api-forbidden")
        counters = envelope["counters"]
        for key in ("credential_reads", "egress", "provider_imports", "writes"):
            self.assertEqual(counters[key], 0, key)
        self.assertFalse(envelope["capabilities"]["codex_generation"]["allowed"])

    def test_paid_capabilities_all_default_denied(self) -> None:
        envelope = denial_envelope(RuntimePolicyError(code="x", detail="y"))
        for name, capability in envelope["capabilities"].items():
            self.assertFalse(capability["allowed"], name)


if __name__ == "__main__":
    unittest.main()
