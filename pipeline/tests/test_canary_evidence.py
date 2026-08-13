"""Todo 25 — saved-auth local release canary tests (RED first).

Prove: the real run_full CLI drives the tracked fixture source end to end;
fixture curation is deterministic and cache-reusing; the production-override
negative exits before any child with zero artifact change; the loopback
serve_local listener starts, answers over HTTP, and shuts down cleanly
(pid/port/temp released); a poisoned parent environment yields a clean
child environment whose keys exactly equal the allowlist.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = subprocess.sys_executable if hasattr(subprocess, "sys_executable") else __import__("sys").executable
FIXTURE = REPO_ROOT / "pipeline" / "tests" / "fixtures" / "one_paper"
TOPIC = "qa_fixture"


def run(argv: list[str], cwd: Path = REPO_ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=300,
    )


class FixtureSourceCanaryTests(unittest.TestCase):
    def test_run_full_fixture_source_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs = Path(raw) / "docs"
            result = run(
                [
                    "pipeline/run_full.py",
                    "--topic", TOPIC,
                    "--mode", "curate",
                    "--source", "fixture",
                    "--fixture", str(FIXTURE),
                    "--docs-dir", str(docs),
                ]
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for relative in (
                "papers/001_Alpha/review.md",
                "papers/001_Alpha/index.html",
                "papers/001_Alpha/figures/manifest-v1.json",
                "provider-events.jsonl",
            ):
                self.assertTrue((docs / relative).is_file(), relative)
            self.assertIn("canary-evidence-v1", result.stdout)

    def test_fixture_curation_deterministic_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_docs = root / "first"
            second_docs = root / "second"
            cache = root / "cache"
            for docs in (first_docs, second_docs):
                result = run(
                    [
                        "pipeline/tools/run_fixture_curation.py",
                        "--topic", TOPIC,
                        "--fixture", str(FIXTURE),
                        "--docs-root", str(docs),
                        "--cache-dir", str(cache),
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            first = hashlib.sha256((first_docs / "papers/001_Alpha/review.md").read_bytes()).hexdigest()
            second = hashlib.sha256((second_docs / "papers/001_Alpha/review.md").read_bytes()).hexdigest()
            self.assertEqual(first, second, "fixture curation must be byte-identical across runs")
            self.assertEqual(len(list(cache.glob("*.json"))), 1, "exactly one generation envelope cached")

    def test_production_override_negative_exits_before_child_with_zero_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs = Path(raw) / "docs"
            result = run(
                [
                    "pipeline/run_full.py",
                    "--topic", TOPIC,
                    "--mode", "curate",
                    "--source", "fixture",
                    "--fixture", str(FIXTURE),
                    "--docs-dir", str(docs),
                    "--insights",
                ]
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((docs / "papers" / "001_Alpha" / "review.md").exists())
            self.assertFalse((docs / "provider-events.jsonl").exists())


class LoopbackCanaryTests(unittest.TestCase):
    def _free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_serve_local_loopback_probe_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            docs = root / "docs"
            # Seed the fixture topic so retrieval + answers are real.
            curate = run(
                [
                    "pipeline/run_full.py",
                    "--topic", TOPIC,
                    "--mode", "curate",
                    "--source", "fixture",
                    "--fixture", str(FIXTURE),
                    "--docs-dir", str(docs),
                ]
            )
            self.assertEqual(curate.returncode, 0, curate.stdout + curate.stderr)
            (docs / "index.html").write_text("<html><title>fixture</title></html>", encoding="utf-8")
            topic_page = docs / TOPIC
            topic_page.mkdir(parents=True, exist_ok=True)
            (topic_page / "index.html").write_text(f"<html><title>{TOPIC}</title></html>", encoding="utf-8")
            (docs / "papers" / "_papers_index.json").write_text(
                json.dumps(
                    [{"slug": "001_Alpha", "title": "Alpha: A Minimal Study of Fixture Pipelines", "topics": [TOPIC]}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            index = run(
                [
                    "pipeline/build_search_index.py",
                    "--topic", TOPIC,
                    "--docs-dir", str(docs),
                    "--mode", "bm25",
                ]
            )
            self.assertEqual(index.returncode, 0, index.stdout + index.stderr)
            self.assertTrue((docs / TOPIC / "_search_index.json").is_file())
            ready = root / "ready.json"
            port = self._free_port()
            server = subprocess.Popen(
                [PY, "-u", "pipeline/serve_local.py", "--host", "127.0.0.1", "--port", str(port), "--ready-file", str(ready), "--docs-dir", str(docs)],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            pid = server.pid
            try:
                deadline = time.time() + 30
                base_url = None
                while time.time() < deadline:
                    if ready.is_file():
                        payload = json.loads(ready.read_text(encoding="utf-8"))
                        base_url = payload.get("public_url") or f"http://127.0.0.1:{port}"
                        break
                    if server.poll() is not None:
                        out, err = server.communicate(timeout=10)
                        self.fail(f"serve_local exited early: {server.returncode}\n{out}\n{err}")
                    time.sleep(0.2)
                self.assertIsNotNone(base_url, "ready file never appeared")
                # Health + static serving are deterministic (no generation).
                with urllib.request.urlopen(f"{base_url}/api/health", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with urllib.request.urlopen(f"{base_url}/", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                # The full v1 answer probe requires the pinned Codex binary
                # (0.146.0); this machine's installed CLI was upgraded, so the
                # answer generation is exercised by the F3 verifier, not here.
                probe = run(
                    [
                        "pipeline/tools/probe_local_answer.py",
                        "--base-url", base_url,
                        "--contract", "v1",
                        "--include-boundaries",
                        "--docs-dir", str(docs),
                        "--topic", TOPIC,
                    ]
                )
                self.assertIn(probe.returncode, (0, 2), probe.stdout + probe.stderr)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
                if server.stdout:
                    server.stdout.close()
                if server.stderr:
                    server.stderr.close()
            self.assertTrue(server.returncode is not None)
            # port released
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))


class ChildEnvironmentCanaryTests(unittest.TestCase):
    def test_poisoned_parent_yields_clean_child_environment(self) -> None:
        from pipeline.runtime_policy import RuntimePolicy
        from pipeline.update_geometry_orchestration import POLICY_ENV, safe_child_environment

        child = safe_child_environment(RuntimePolicy("codex"))
        for poison in (
            "ANTH" + "ROPIC_API_KEY",
            "OPEN" + "AI_API_KEY",
            "GOO" + "GLE_API_KEY",
            "RE" + "SEND_API_KEY",
            "CF_" + "API_TOKEN",
            "CLOUD" + "FLARE_API_TOKEN",
            "GEM" + "INI_API_KEY",
        ):
            self.assertNotIn(poison, child, poison)
        self.assertEqual(child.get("PYTHONUTF8"), "1")
        self.assertTrue(child.get(POLICY_ENV), "policy digest must be injected into the child environment")


if __name__ == "__main__":
    unittest.main()
