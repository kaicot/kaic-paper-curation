"""Todo 24 — one-paper Codex E2E tests (RED first).

Drive the real production run-state + generation-cache machinery against
the tracked one-paper fixture with a counting fake generator: first run
produces every required artifact with exactly one generation event; a
second run issues zero events and is byte-identical; changing the source
identity recomputes; busy contention raises TopicBusyError.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.lib.generation_cache import CacheIdentity, CacheSuccess, GenerationCache
from pipeline.lib.run_state import RunRequest, RunStateStore, RunStatus, TopicBusyError
from pipeline.runtime_policy import RuntimePolicy
from pipeline.update_geometry_orchestration import policy_digest

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "one_paper"
TOPIC = "fixture"
SLUG = "001_Alpha"


def make_identity(source: bytes, prompt_version: str = "v1") -> CacheIdentity:
    return CacheIdentity(
        runtime_mode="codex",
        capability="codex_generation",
        role="terra",
        model="gpt-5.6-terra",
        reasoning_effort="xhigh",
        cli_version="0.146.0",
        signed_binary_sha256="0" * 64,
        attestation_sha256="0" * 64,
        contract_sha256="0" * 64,
        policy_version="runtime-v2/codex-v1",
        policy_sha256="0" * 64,
        prompt_version=prompt_version,
        prompt_sha256=hashlib.sha256(b"review prompt").hexdigest(),
        schema_version="review-schema-v1",
        schema_sha256="0" * 64,
        source_sha256=hashlib.sha256(source).hexdigest(),
        task_id="safe-review",
    )


def build_workspace(root: Path) -> dict:
    """Seed a temp workspace from the fixture; return paths."""
    docs = root / "docs"
    paper_dir = docs / "papers" / SLUG
    paper_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "seed" / "papers" / SLUG / "text.md", paper_dir / "text.md")
    shutil.copyfile(FIXTURE / "seed" / "papers" / SLUG / "metadata.json", paper_dir / "metadata.json")
    topic_dir = docs / TOPIC
    topic_dir.mkdir(parents=True)
    (topic_dir / "figures").mkdir(exist_ok=True)
    return {
        "docs": docs,
        "paper_dir": paper_dir,
        "topic_dir": topic_dir,
        "state": root / "state",
        "cache": root / "cache",
        "events": docs / "provider-events.jsonl",
    }


def render_review(result: dict, paper_dir: Path) -> None:
    review = paper_dir / "review.md"
    body = result["review"]
    review.write_text(f"# {result['title']}\n\n{body}\n", encoding="utf-8")
    (paper_dir / "index.html").write_text(
        f"<!doctype html><title>{result['title']}</title><main>{body}</main>", encoding="utf-8"
    )
    manifest = paper_dir / "figures"
    manifest.mkdir(exist_ok=True)
    (manifest / "manifest-v1.json").write_text(
        json.dumps({"schema": "figure-manifest-v1", "figures": []}), encoding="utf-8"
    )


def digest_of(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class OnePaperCodexE2ETests(unittest.TestCase):
    def _run_stage(self, workspace: dict, cache: GenerationCache, counting: list[int], source: bytes) -> None:
        identity = make_identity(source)
        result = cache.get_or_generate(
            identity,
            lambda: (counting.append(1), CacheSuccess(result={"title": "Alpha", "review": "리뷰 본문 (E2E)"}))[1],
        )
        render_review(result, Path(workspace["paper_dir"]))
        with Path(workspace["events"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"capability": "codex_generation", "role": "terra"}) + "\n")

    def test_first_run_produces_all_artifacts_with_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = build_workspace(Path(raw))
            cache = GenerationCache(Path(workspace["cache"]))
            counting: list[int] = []
            source = Path(workspace["paper_dir"] / "text.md").read_bytes()
            self._run_stage(workspace, cache, counting, source)
            for relative in ("papers/001_Alpha/review.md", "papers/001_Alpha/index.html", "papers/001_Alpha/figures/manifest-v1.json", "provider-events.jsonl"):
                self.assertTrue((Path(workspace["docs"]) / relative).is_file(), relative)
            self.assertEqual(len(counting), 1)
            self.assertEqual(len(Path(workspace["events"]).read_text(encoding="utf-8").splitlines()), 1)

    def test_second_run_zero_calls_and_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = build_workspace(Path(raw))
            cache = GenerationCache(Path(workspace["cache"]))
            counting: list[int] = []
            source = Path(workspace["paper_dir"] / "text.md").read_bytes()
            self._run_stage(workspace, cache, counting, source)
            first = {name: digest_of(Path(workspace["docs"]) / name) for name in
                     ("papers/001_Alpha/review.md", "papers/001_Alpha/index.html", "papers/001_Alpha/figures/manifest-v1.json")}
            self._run_stage(workspace, cache, counting, source)
            self.assertEqual(len(counting), 1, "second run must not call the generator")
            second = {name: digest_of(Path(workspace["docs"]) / name) for name in first}
            self.assertEqual(first, second)
            self.assertEqual(len(Path(workspace["events"]).read_text(encoding="utf-8").splitlines()), 2)

    def test_source_identity_change_recomputes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = build_workspace(Path(raw))
            cache = GenerationCache(Path(workspace["cache"]))
            counting: list[int] = []
            source = Path(workspace["paper_dir"] / "text.md").read_bytes()
            self._run_stage(workspace, cache, counting, source)
            self.assertEqual(len(counting), 1)
            changed = source + b"\n## Addendum\n"
            self._run_stage(workspace, cache, counting, changed)
            self.assertEqual(len(counting), 2, "changed source identity must recompute")

    def test_busy_contention_raises_topic_busy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = build_workspace(Path(raw))
            store = RunStateStore(Path(workspace["state"]))
            policy = RuntimePolicy("codex")
            lease = store.acquire(RunRequest.create(TOPIC, "safe-e2e", policy_digest(policy)))
            try:
                with self.assertRaises(TopicBusyError):
                    store.acquire(RunRequest.create(TOPIC, "safe-e2e", policy_digest(policy)))
            finally:
                lease.finish(RunStatus.SUCCEEDED)
                lease.release()
            next_lease = store.acquire(RunRequest.create(TOPIC, "safe-e2e", policy_digest(policy)))
            next_lease.finish(RunStatus.SUCCEEDED)
            next_lease.release()


if __name__ == "__main__":
    unittest.main()
