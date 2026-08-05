"""Safe retained-stage orchestration, geometry, MOC, and resume acceptance."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, final
from unittest.mock import patch

from pipeline.geometry_figures import publish_geometry_manifest
from pipeline.lib.run_state import RunStateError, RunStateStore
from pipeline.runtime_policy import RuntimePolicy
from pipeline.update_geometry_orchestration import (
    POLICY_ENV,
    Stage,
    artifact_manifest,
    build_transitional_sparse_index,
    default_stage_plan,
    execute_plan,
    policy_digest,
    safe_child_environment,
)


moc_module = importlib.import_module("pipeline.generate_moc")
update_module = importlib.import_module("pipeline.run_update_force")
do_process = cast(
    Callable[..., tuple[str, str]],
    getattr(update_module, "_do_process"),
)
retained_moc = cast(
    Callable[[str, str | Path], dict[str, str]],
    getattr(moc_module, "_retained_moc"),
)
generate_moc_insights = cast(
    Callable[[str, str | Path], dict[str, str]],
    getattr(moc_module, "generate_moc_insights"),
)


def _write_output(path: Path, stage: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        payload: object = {"schema": f"fixture-{stage}"}
        if path.name == "manifest-v1.json":
            payload = {
                "rows": [],
                "schema": "geometry-figures-v1",
                "source_pdf_sha256": "0" * 64,
            }
        _ = path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        _ = path.write_text(f"{stage}\n", encoding="utf-8")


@final
class RecordingRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        fail_stage: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.fail_stage = fail_stage
        self.calls: list[str] = []

    def __call__(self, stage: Stage) -> None:
        self.calls.append(stage.name)
        if stage.name == self.fail_stage:
            raise RuntimeError(f"failed:{stage.name}")
        for relative in stage.outputs:
            _write_output(self.workspace / relative, stage.name)


@final
class SafeUpdateOrchestrationTests(unittest.TestCase):
    def test_corrupt_active_marker_uses_typed_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="corrupt-resume-") as directory:
            workspace = Path(directory)
            state_root = workspace / ".state"
            store = RunStateStore(state_root, workspace_root=workspace)
            marker = store.marker_path("fixture")
            _ = marker.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RunStateError) as caught:
                _ = execute_plan(
                    (),
                    topic="fixture",
                    policy=RuntimePolicy("codex"),
                    state_root=state_root,
                    workspace_root=workspace,
                    runner=RecordingRunner(workspace),
                    resume=True,
                )
            self.assertEqual(caught.exception.code, "resume-required")
            self.assertEqual(
                len(list((state_root / "quarantine").iterdir())),
                1,
            )
            recovered = cast(
                dict[str, object],
                json.loads(marker.read_text(encoding="utf-8")),
            )
            self.assertTrue(
                str(recovered["run_id"]).startswith("recovery-")
            )

    def test_transitional_bm25_sidecar_is_sparse_and_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparse-sidecar-") as directory:
            workspace = Path(directory)
            paper_index = workspace / "docs/papers/_papers_index.json"
            paper_index.parent.mkdir(parents=True)
            _ = paper_index.write_text(
                json.dumps(
                    [
                        {
                            "abstract": "agent model agent",
                            "essence": "local retrieval",
                            "slug": "001_Agent",
                            "title": "Agent Model",
                            "topics": ["fixture"],
                        },
                        {
                            "slug": "002_Other",
                            "title": "Other",
                            "topics": ["other"],
                        },
                    ],
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            first = build_transitional_sparse_index("fixture", workspace)
            first_bytes = first.read_bytes()
            second = build_transitional_sparse_index("fixture", workspace)
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, second.read_bytes())
            value = cast(
                dict[str, object],
                json.loads(first_bytes),
            )
            self.assertEqual(value["schema_version"], 2)
            self.assertIn("sparse", str(value["schema"]))
            self.assertFalse(
                (workspace / "docs/fixture/_search_index.json").exists()
            )

    def test_review_path_always_publishes_empty_geometry_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="geometry-review-") as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            _ = pdf.write_bytes(b"%PDF-review-geometry")
            paper = root / "001_Paper"
            paper.mkdir()

            def extract_text(_pdf: str, slug_dir: str) -> None:
                _ = (Path(slug_dir) / "text.md").write_text(
                    "validated paper text " * 20,
                    encoding="utf-8",
                )

            def write_review(
                _item: object,
                slug_dir: str,
                _figures: object,
                *,
                runtime_policy: object,
            ) -> bool:
                _ = runtime_policy
                _ = (Path(slug_dir) / "review.md").write_text(
                    "validated review " * 30,
                    encoding="utf-8",
                )
                return True

            def convert_html(_slug: str) -> None:
                _ = (paper / "index.html").write_text(
                    "<html>validated</html>" * 20,
                    encoding="utf-8",
                )

            with (
                patch.object(update_module, "extract_text", extract_text),
                patch.object(
                    update_module,
                    "_zotero_text_sanity",
                    return_value=(True, ""),
                ),
                patch.object(
                    update_module,
                    "extract_figures",
                    side_effect=RuntimeError("geometry unavailable"),
                ),
                patch.object(update_module, "write_review", write_review),
                patch.object(update_module, "convert_to_html", convert_html),
                patch.dict(
                    os.environ,
                    {"GOO" + "GLE_API_KEY": "poison"},
                    clear=False,
                ),
            ):
                status, reason = do_process(
                    {},
                    "001_Paper",
                    str(paper),
                    str(pdf),
                    runtime_policy=RuntimePolicy("codex"),
                )
            self.assertEqual((status, reason), ("ok", ""))
            manifest = cast(
                dict[str, object],
                json.loads(
                    (paper / "figures/manifest-v1.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
            self.assertEqual(manifest["schema"], "geometry-figures-v1")
            self.assertEqual(manifest["rows"], [])

    def test_exact_safe_plan_policy_and_named_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="safe-plan-") as directory:
            workspace = Path(directory)
            policy = RuntimePolicy("codex")
            executable = str(workspace / "python.exe")
            plan = default_stage_plan(
                "fixture",
                policy,
                executable=executable,
                paper_slugs=["001_Paper"],
            )
            self.assertEqual(
                [stage.name for stage in plan],
                [
                    "review",
                    "geometry",
                    "build-papers-index",
                    "classification",
                    "summary",
                    "connection",
                    "timeline",
                    "html",
                    "bm25",
                    "topic-index",
                    "rss",
                    "moc",
                ],
            )
            child_stages = [stage for stage in plan if stage.argv]
            self.assertTrue(
                all(stage.argv[0] == executable for stage in child_stages)
            )
            policy_stages = {
                stage.name: stage
                for stage in plan
                if stage.name
                in {"classification", "summary", "connection", "timeline"}
            }
            for stage in policy_stages.values():
                self.assertEqual(stage.argv.count("--llm-mode"), 1)
                mode_index = stage.argv.index("--llm-mode")
                self.assertEqual(stage.argv[mode_index + 1], "codex")
            self.assertIn("--only", policy_stages["connection"].argv)
            self.assertIn("connections", policy_stages["connection"].argv)
            self.assertIn("--images", policy_stages["timeline"].argv)
            self.assertIn("skip", policy_stages["timeline"].argv)
            joined = "\n".join(" ".join(stage.argv) for stage in plan)
            self.assertNotIn("_insights.json", joined)
            self.assertNotIn("prepare_deploy", joined)
            self.assertNotIn("generate_network", joined)

            environment = safe_child_environment(policy)
            self.assertEqual(environment[POLICY_ENV], policy_digest(policy))
            runner = RecordingRunner(workspace)
            blocked_keys = {
                "GOO" + "GLE_API_KEY",
                "OPEN" + "AI_API_KEY",
                "CLOUD" + "FLARE_API_TOKEN",
            }
            with patch.dict(
                os.environ,
                {
                    "GOO" + "GLE_API_KEY": "poison",
                    "OPEN" + "AI_API_KEY": "poison",
                    "CLOUD" + "FLARE_API_TOKEN": "poison",
                },
                clear=False,
            ):
                poisoned_environment = safe_child_environment(policy)
                self.assertTrue(
                    blocked_keys.isdisjoint(poisoned_environment)
                )
                result = execute_plan(
                    plan,
                    topic="fixture",
                    policy=policy,
                    state_root=workspace / ".state",
                    workspace_root=workspace,
                    runner=runner,
                )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(runner.calls, [stage.name for stage in plan])
            geometry_relative = (
                "docs/papers/001_Paper/figures/manifest-v1.json"
            )
            manifest = artifact_manifest(
                "fixture",
                policy,
                workspace,
                geometry_paths=[geometry_relative],
            )
            names = {
                cast(str, row["name"])
                for row in cast(list[dict[str, object]], manifest["outputs"])
            }
            self.assertTrue(
                {
                    "classification",
                    "summary",
                    "connection",
                    "timeline",
                    "geometry-manifest",
                    "html",
                    "bm25",
                    "rss",
                    "moc-insights",
                }.issubset(names)
            )

    def test_summary_failure_preserves_review_and_resume_starts_at_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="safe-resume-") as directory:
            workspace = Path(directory)
            policy = RuntimePolicy("codex")
            plan = default_stage_plan(
                "fixture",
                policy,
                executable=str(workspace / "python.exe"),
                paper_slugs=["001_Paper"],
            )
            first = RecordingRunner(workspace, fail_stage="summary")
            with self.assertRaisesRegex(RuntimeError, "failed:summary"):
                _ = execute_plan(
                    plan,
                    topic="fixture",
                    policy=policy,
                    state_root=workspace / ".state",
                    workspace_root=workspace,
                    runner=first,
                )
            review = workspace / "docs/papers/001_Paper/review.md"
            review_hash = hashlib.sha256(review.read_bytes()).hexdigest()
            self.assertEqual(
                first.calls,
                [
                    "review",
                    "geometry",
                    "build-papers-index",
                    "classification",
                    "summary",
                ],
            )
            self.assertFalse(
                (workspace / "docs/fixture/_paper_connections.json").exists()
            )
            run_path = next((workspace / ".state/runs").glob("*.json"))
            failed_record = cast(
                dict[str, object],
                json.loads(run_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(failed_record["status"], "failed")
            self.assertEqual(
                failed_record["policy_digest"],
                policy_digest(policy),
            )
            failed_stages = cast(
                dict[str, dict[str, object]],
                failed_record["stages"],
            )
            summary_inputs = cast(
                dict[str, str],
                failed_stages["summary"]["inputs"],
            )
            self.assertEqual(
                summary_inputs["policy_sha256"],
                policy_digest(policy),
            )
            self.assertEqual(len(summary_inputs["argv_sha256"]), 64)

            resumed = RecordingRunner(workspace)
            result = execute_plan(
                plan,
                topic="fixture",
                policy=policy,
                state_root=workspace / ".state",
                workspace_root=workspace,
                runner=resumed,
                resume=True,
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(resumed.calls[0], "summary")
            self.assertNotIn("review", resumed.calls)
            self.assertEqual(
                hashlib.sha256(review.read_bytes()).hexdigest(),
                review_hash,
            )
            succeeded_record = cast(
                dict[str, object],
                json.loads(run_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(succeeded_record["status"], "succeeded")

    def test_geometry_manifest_nonempty_empty_and_provider_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="geometry-") as directory:
            root = Path(directory)
            pdf = root / "source.pdf"
            _ = pdf.write_bytes(b"%PDF-geometry-fixture")
            paper = root / "docs/papers/001_Paper"
            figure = paper / "figures/fig2.png"
            figure.parent.mkdir(parents=True)
            _ = figure.write_bytes(b"deterministic-image")
            with patch.dict(
                os.environ,
                {"GOO" + "GLE_API_KEY": "raise-if-read"},
                clear=False,
            ):
                populated = publish_geometry_manifest(
                    pdf,
                    paper,
                    [{"caption": "Figure 2.", "name": "2", "page": 3}],
                )
            self.assertEqual(populated["schema"], "geometry-figures-v1")
            self.assertEqual(
                populated["source_pdf_sha256"],
                hashlib.sha256(pdf.read_bytes()).hexdigest(),
            )
            rows = cast(list[dict[str, object]], populated["rows"])
            self.assertEqual(
                rows,
                [
                    {
                        "caption": "Figure 2.",
                        "page": 3,
                        "path": "figures/fig2.png",
                        "sha256": hashlib.sha256(figure.read_bytes()).hexdigest(),
                    }
                ],
            )
            empty_paper = root / "docs/papers/002_Paper"
            empty = publish_geometry_manifest(pdf, empty_paper, [])
            self.assertEqual(empty["rows"], [])
            self.assertTrue(
                (empty_paper / "figures/manifest-v1.json").is_file()
            )

    def test_moc_fallback_uses_three_retained_inputs_deterministically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc-retained-") as directory:
            topic = Path(directory)
            summaries: list[dict[str, object]] = [
                {
                    "category": "Models",
                    "description_ko": "모델 연구 신호",
                    "papers": [{"slug": "002_Model", "title": "Model"}],
                },
                {
                    "category": "Agents",
                    "description_ko": "에이전트 연구 신호",
                    "papers": [{"slug": "001_Agent", "title": "Agent"}],
                },
            ]
            connections: dict[str, list[dict[str, object]]] = {
                "002_Model": [
                    {
                        "reason": "shared evidence",
                        "relation": "extends",
                        "slug": "001_Agent",
                    }
                ],
                "001_Agent": [
                    {
                        "reason": "shared evidence",
                        "relation": "extends",
                        "slug": "002_Model",
                    }
                ],
            }
            timeline: dict[str, object] = {
                "category_analyses": {
                    "Models": {"sub_themes": []},
                    "Agents": {
                        "current_state_summary": "검증 중심으로 발전한다.",
                        "sub_themes": [],
                    },
                },
                "executive_summary_ko": "두 분야는 검증 가능한 연결을 중심으로 발전한다.",
            }
            retained_inputs: tuple[tuple[str, object], ...] = (
                ("_category_summaries.json", summaries),
                ("_paper_connections.json", connections),
                ("_timeline_narrative.json", timeline),
            )
            for name, value in retained_inputs:
                _ = (topic / name).write_text(
                    json.dumps(value, ensure_ascii=False),
                    encoding="utf-8",
                )
            with patch.object(
                moc_module,
                "PAPERS_DIR",
                side_effect=AssertionError("paper index must not be read"),
            ):
                first = retained_moc("fixture", topic)
                first_bytes = (topic / "MOC_Insights.md").read_bytes()
                second = retained_moc("fixture", topic)
            self.assertEqual(first["renderer"], "moc-insights-v1")
            self.assertEqual(second["status"], "ok")
            self.assertEqual(
                first_bytes,
                (topic / "MOC_Insights.md").read_bytes(),
            )
            text = first_bytes.decode("utf-8")
            self.assertIn("schema: moc-insights-v1", text)
            self.assertEqual(text.count("shared evidence"), 1)
            self.assertLess(text.index("### Agents"), text.index("### Models"))
            self.assertFalse((topic / "_insights.json").exists())
            _ = (topic / "_insights.json").write_text(
                '{"cross_category":[{"type":"gap"}]}',
                encoding="utf-8",
            )
            safe_result = generate_moc_insights("fixture", topic)
            self.assertEqual(safe_result["renderer"], "moc-insights-v1")
            self.assertIn(
                "schema: moc-insights-v1",
                (topic / "MOC_Insights.md").read_text(encoding="utf-8"),
            )

            prior = (topic / "MOC_Insights.md").read_bytes()
            _ = (topic / "_paper_connections.json").write_text(
                "[]",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = retained_moc("fixture", topic)
            self.assertEqual(
                prior,
                (topic / "MOC_Insights.md").read_bytes(),
            )


if __name__ == "__main__":
    _ = unittest.main()
