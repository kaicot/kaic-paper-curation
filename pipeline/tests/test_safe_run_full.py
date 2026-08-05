from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pipeline import run_full
from pipeline.run_full import RunOptions
from pipeline.release_dry_run import (
    DEFAULT_VALIDATOR_STAGES,
    ArtifactValidationError,
    validate_default_artifacts,
)
from pipeline.sparse_index import sparse_payload
from pipeline.lib.run_state import (
    RunRequest,
    RunStateStore,
    RunStatus,
    TopicBusyError,
)
from pipeline.runtime_policy import RuntimePolicy
from pipeline.update_geometry_orchestration import (
    Stage,
    artifact_manifest,
    execute_plan,
    policy_digest,
)


TOPIC = "qa_fixture"
SLUG = "001_Alpha"


def _write_json(path: Path, value: object) -> None:
    _ = path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _fixture(root: Path) -> tuple[Path, dict[str, Path]]:
    docs = root / "docs"
    paper = docs / "papers" / SLUG
    figures = paper / "figures"
    topic = docs / TOPIC
    _ = figures.mkdir(parents=True)
    _ = topic.mkdir(parents=True)
    text = paper / "text.md"
    _ = text.write_text("source paper alpha\n", encoding="utf-8")
    text_sha = hashlib.sha256(text.read_bytes()).hexdigest()
    review = paper / "review.md"
    _ = review.write_text(
        """---
schema_version: v1
---
# Alpha

## Essence
Alpha agent evidence.

## Motivation
- **Known**: known
- **Gap**: gap
- **Why**: why
- **Approach**: approach

## Achievement
1. result

## How
- method

## Originality
- novelty

## Limitation & Further Study
- limit

## Evaluation
- Novelty: 4/5
- Technical Soundness: 4/5
- Significance: 4/5
- Clarity: 4/5
- Overall: 4/5

**총평**: useful
""",
        encoding="utf-8",
    )
    _ = (paper / "index.html").write_text(
        "<!doctype html><title>Alpha</title><main>review</main>",
        encoding="utf-8",
    )
    image = figures / "fig1.png"
    _ = image.write_bytes(b"geometry")
    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    _write_json(
        figures / "manifest-v1.json",
        {
            "rows": [
                {
                    "caption": "Figure 1.",
                    "page": 0,
                    "path": "figures/fig1.png",
                    "sha256": image_sha,
                }
            ],
            "schema": "geometry-figures-v1",
            "source_pdf_sha256": "a" * 64,
        },
    )
    papers_index = docs / "papers" / "_papers_index.json"
    _write_json(
        papers_index,
        [
            {
                "classifications": {
                    TOPIC: {
                        "all_categories": ["Agents"],
                        "primary_category": "Agents",
                        "sub_category": "Alpha",
                    }
                },
                "slug": SLUG,
                "text_md_sha256": text_sha,
                "title": "Alpha",
                "topics": [TOPIC],
            }
        ],
    )
    _write_json(
        topic / "_new_classification.json",
        {
            "assignments": {
                SLUG: {
                    "all_categories": ["Agents"],
                    "primary_category": "Agents",
                    "sub_category": "Alpha",
                }
            },
            "categories": [{"name": "Agents"}],
        },
    )
    _write_json(
        topic / "_category_summaries.json",
        [
            {
                "category": "Agents",
                "description_ko": "에이전트 연구 요약입니다.",
                "papers": [{"slug": SLUG, "title": "Alpha"}],
                "source_sha256": "b" * 64,
                "sub_themes": [],
            }
        ],
    )
    _write_json(topic / "_paper_connections.json", {SLUG: []})
    _write_json(
        topic / "_category_narratives.json",
        [
            {
                "caption": "caption",
                "category": "Agents",
                "method_text": "method",
                "source_sha256": "c" * 64,
                "summary": "summary",
            }
        ],
    )
    _write_json(
        topic / "_timeline_narrative.json",
        {
            "category_analyses": {
                "Agents": {
                    "current_state_summary": "current",
                    "sub_themes": [],
                }
            },
            "executive_summary_ko": "전체 흐름입니다.",
        },
    )
    _write_json(
        topic / "_search_index.json",
        sparse_payload(TOPIC, papers_index),
    )
    _ = (topic / "index.html").write_text(
        "<!doctype html><title>qa_fixture</title><main>Alpha</main>",
        encoding="utf-8",
    )
    _ = (topic / "feed.xml").write_text(
        """<?xml version="1.0"?><rss version="2.0"><channel><title>qa_fixture</title><item><title>Alpha</title></item></channel></rss>""",
        encoding="utf-8",
    )
    _ = (topic / "MOC_Insights.md").write_text(
        """# Research Insights — qa_fixture

*Source: moc-insights-v1 (deterministic retained-artifact fallback)*
""",
        encoding="utf-8",
    )
    _ = (topic / "MOC_Categories.md").write_text(
        "# Category Map — qa_fixture\n\n## Agents\n",
        encoding="utf-8",
    )
    return docs, {
        "review": review,
        "geometry": figures / "manifest-v1.json",
        "paper-index": papers_index,
        "classification": topic / "_new_classification.json",
        "summary": topic / "_category_summaries.json",
        "connection": topic / "_paper_connections.json",
        "timeline": topic / "_timeline_narrative.json",
        "html": paper / "index.html",
        "bm25": topic / "_search_index.json",
        "topic-index": topic / "index.html",
        "rss": topic / "feed.xml",
        "moc": topic / "MOC_Insights.md",
    }


class SafeRunFullTests(unittest.TestCase):
    def test_exact_twelve_artifact_validators_and_each_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs, paths = _fixture(Path(raw))
            result = validate_default_artifacts(TOPIC, docs)
            self.assertEqual(
                [row["stage"] for row in result],
                list(DEFAULT_VALIDATOR_STAGES),
            )
            self.assertTrue(all(row["status"] == "valid" for row in result))
            manifest = artifact_manifest(
                TOPIC,
                RuntimePolicy("codex"),
                Path(raw),
                validators=result,
            )
            self.assertEqual(manifest["validator_count"], 12)
            self.assertEqual(
                [
                    row["stage"]
                    for row in cast(
                        list[dict[str, object]],
                        manifest["validators"],
                    )
                ],
                list(DEFAULT_VALIDATOR_STAGES),
            )

            for stage in DEFAULT_VALIDATOR_STAGES:
                with self.subTest(stage=stage):
                    original = paths[stage].read_bytes()
                    _ = paths[stage].write_bytes(b"")
                    with self.assertRaises(ArtifactValidationError) as caught:
                        _ = validate_default_artifacts(TOPIC, docs)
                    self.assertEqual(caught.exception.stage, stage)
                    _ = paths[stage].write_bytes(original)

    def test_safe_defaults_use_one_worker_and_never_deploy(self) -> None:
        parser = run_full.build_parser()
        args = cast(
            RunOptions,
            cast(object, parser.parse_args(["--topic", TOPIC])),
        )
        self.assertEqual(args.mode, "curate")
        self.assertEqual(args.source, "zotero")
        self.assertEqual(args.images, "skip")
        self.assertEqual(args.concurrency, 1)
        choices = parser._option_string_actions["--mode"].choices
        self.assertIsNotNone(choices)
        assert choices is not None
        self.assertNotIn("deploy", choices)

    def test_off_mode_returns_exact_three_without_child_or_artifact_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs, _ = _fixture(Path(raw))
            before = {
                path.relative_to(docs).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in docs.rglob("*")
                if path.is_file()
            }
            calls: list[list[str]] = []
            output = io.StringIO()

            def unexpected_runner(
                command: Sequence[str],
                timeout: float | None = None,
                env: dict[str, str] | None = None,
            ) -> int:
                del timeout, env
                calls.append(list(command))
                return 0

            with contextlib.redirect_stdout(output):
                result = run_full.main(
                    [
                        "--topic",
                        TOPIC,
                        "--llm-mode",
                        "off",
                        "--docs-dir",
                        str(docs),
                    ],
                    runner=unexpected_runner,
                )
            after = {
                path.relative_to(docs).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in docs.rglob("*")
                if path.is_file()
            }
            payload = cast(
                dict[str, object],
                json.loads(output.getvalue()),
            )
            self.assertEqual(result, 3)
            self.assertEqual(payload["schema"], "run-result-v1")
            self.assertEqual(payload["status"], "policy_denied")
            self.assertEqual(payload["completed_deterministic_stages"], [])
            self.assertEqual(calls, [])
            self.assertEqual(after, before)

    def test_codex_success_requires_artifact_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs, paths = _fixture(Path(raw))
            calls: list[list[str]] = []

            def runner(
                command: Sequence[str],
                timeout: float | None = None,
                env: dict[str, str] | None = None,
            ) -> int:
                del timeout, env
                calls.append(list(command))
                return 0

            result = run_full.main(
                [
                    "--topic",
                    TOPIC,
                    "--llm-mode",
                    "codex",
                    "--no-sync",
                    "--docs-dir",
                    str(docs),
                ],
                runner=runner,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].count("--llm-mode"), 1)
            self.assertEqual(calls[0][calls[0].index("--concurrency") + 1], "1")
            _ = paths["rss"].write_text("", encoding="utf-8")

            def successful_runner(
                command: Sequence[str],
                timeout: float | None = None,
                env: dict[str, str] | None = None,
            ) -> int:
                del command, timeout, env
                return 0

            self.assertEqual(
                run_full.main(
                    [
                        "--topic",
                        TOPIC,
                        "--llm-mode",
                        "codex",
                        "--no-sync",
                        "--docs-dir",
                        str(docs),
                    ],
                    runner=successful_runner,
                ),
                1,
            )

    def test_unfinalized_plan_keeps_topic_lease_through_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            policy = RuntimePolicy("codex")
            store = RunStateStore(state, workspace_root=workspace)
            # Use the executor's real policy digest in the owned lease identity.
            request = RunRequest.create(
                TOPIC,
                "safe-update",
                policy_digest(policy),
            )
            lease = store.acquire(request)
            stage = Stage(
                "publish",
                ("fixture",),
                ("artifact.txt",),
                ("artifact",),
            )

            def publish(_stage: Stage) -> None:
                _ = (workspace / "artifact.txt").write_text(
                    "accepted",
                    encoding="utf-8",
                )

            result = execute_plan(
                (stage,),
                topic=TOPIC,
                policy=policy,
                state_root=state,
                workspace_root=workspace,
                runner=publish,
                lease=lease,
                finalize=False,
            )
            events = cast(list[dict[str, str]], result["events"])
            self.assertEqual(events[0]["status"], "succeeded")
            self.assertFalse(lease.finished)
            with self.assertRaises(TopicBusyError):
                _ = store.acquire(
                    RunRequest.create(
                        TOPIC,
                        "safe-update",
                        policy_digest(policy),
                    )
                )
            lease.finish(RunStatus.SUCCEEDED)
            lease.release()
            next_lease = store.acquire(
                RunRequest.create(
                    TOPIC,
                    "safe-update",
                    policy_digest(policy),
                )
            )
            next_lease.finish(RunStatus.SUCCEEDED)
            next_lease.release()

    def test_second_real_update_process_exits_seventy_five_while_locked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            policy = RuntimePolicy("codex")
            store = RunStateStore(state)
            lease = store.acquire(
                RunRequest.create(
                    TOPIC,
                    "safe-update",
                    policy_digest(policy),
                )
            )
            environment = os.environ.copy()
            environment["PAPER_CURATION_PY312"] = sys.executable
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(
                            Path(__file__).resolve().parents[1]
                            / "run_update_force.py"
                        ),
                        "--topic",
                        TOPIC,
                        "--llm-mode",
                        "codex",
                        "--state-dir",
                        str(state),
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 75, result.stderr)
            finally:
                lease.finish(RunStatus.SUCCEEDED)
                lease.release()


if __name__ == "__main__":
    _ = unittest.main()
