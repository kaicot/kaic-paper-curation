"""Task independence and semantic cache reuse across qualified CLI updates."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline.model_config import ROOT, TASKS, load_model_config, load_role_models, role_fingerprint
from pipeline.lib.generation_cache import CacheSuccess, GenerationCache
from pipeline.tests.test_generation_cache import _identity


class ModelConfigurationTests(unittest.TestCase):
    def test_default_tasks_are_separate_and_no_paid_or_fast_setting(self):
        models = load_role_models()
        self.assertEqual(models["review"], ("gpt-5.6-terra", "high"))
        self.assertEqual(models["topic_labels"], ("gpt-5.6-luna", "medium"))
        self.assertEqual(set(load_model_config()["roles"]), set(TASKS))

    def test_review_change_does_not_invalidate_luna_task(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pipeline").mkdir()
            cfg = load_model_config()
            path = root / "pipeline/model-config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            before = role_fingerprint("topic_labels", root)
            review_before = role_fingerprint("review", root)
            cfg["roles"]["review"]["model"] = "gpt-6-astra"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertEqual(before, role_fingerprint("topic_labels", root))
            self.assertNotEqual(review_before, role_fingerprint("review", root))
            cfg["roles"]["review"]["reasoning_effort"] = "none"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_role_models(root)

    def test_qualified_cli_patch_reuses_result_preserving_original_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = GenerationCache(Path(raw))
            first = replace(_identity(), compatibility_epoch="generation-contract-v2")
            cache.get_or_generate(first, lambda: CacheSuccess({"answer": "verified"}))
            second = replace(first, cli_version="99.1.0", signed_binary_sha256="a" * 64,
                             attestation_sha256="b" * 64)
            self.assertEqual(cache.load(second), {"answer": "verified"})
            envelope = json.loads((Path(raw) / (first.digest + ".json")).read_text())
            self.assertEqual(envelope["identity"]["cli_version"], first.cli_version)
            self.assertEqual(cache.last_provenance["cli_version"], first.cli_version)
            self.assertIsNone(cache.load(replace(second, contract_sha256="c" * 64)))
            self.assertIsNone(cache.load(replace(second, model="gpt-6-astra")))
            self.assertIsNone(cache.load(replace(second, policy_sha256="d" * 64)))
            envelope["identity"]["cli_version"] = "forged"
            (Path(raw) / (first.digest + ".json")).write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            self.assertIsNone(cache.load(second))


if __name__ == "__main__":
    unittest.main()
