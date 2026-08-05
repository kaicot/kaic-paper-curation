"""Pure safe-update planning, checkpointing, and artifact manifest helpers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pipeline.lib.run_state import (
    RunRequest,
    RunLease,
    RunStateError,
    RunStateStore,
    RunStatus,
)
from pipeline.runtime_policy import RuntimePolicy


def _atomic_write_json(path: Path, value: object) -> None:
    _ = path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    _ = temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)

POLICY_ENV: Final = "PAPER_CURATION_POLICY_SHA256"


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def policy_digest(policy: RuntimePolicy) -> str:
    return canonical_sha256(policy.envelope())


def approved_python() -> str:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() not in {"python.exe", "python3.12", "python"}:
        raise RuntimeError("approved Python executable is unavailable")
    return str(executable)


POLICY_SCRIPTS: Final = {
    "pipeline/topic_modeling.py",
    "pipeline/build_category_summaries.py",
    "pipeline/extract_insights.py",
    "pipeline/generate_timelines.py",
}


def child_argv(
    executable: str,
    script: str,
    arguments: Sequence[str],
    policy: RuntimePolicy,
) -> tuple[str, ...]:
    argv = [executable, script, *arguments]
    if script in POLICY_SCRIPTS and "--llm-mode" not in argv:
        argv.extend(["--llm-mode", policy.mode])
    if script == "pipeline/extract_insights.py":
        if "--only" not in argv:
            argv.extend(["--only", "connections"])
    if script == "pipeline/generate_timelines.py":
        if "--images" not in argv:
            argv.extend(["--images", "skip"])
    return tuple(argv)


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    argv: tuple[str, ...]
    outputs: tuple[str, ...]
    output_names: tuple[str, ...]


def default_stage_plan(
    topic: str,
    policy: RuntimePolicy,
    *,
    executable: str | None = None,
    paper_slugs: Sequence[str] = (),
) -> tuple[Stage, ...]:
    py = executable or approved_python()
    topic_root = f"docs/{topic}"
    review_outputs = tuple(
        f"docs/papers/{slug}/review.md" for slug in sorted(paper_slugs)
    ) or ("docs/papers/_safe-review.marker.json",)
    geometry_outputs = tuple(
        f"docs/papers/{slug}/figures/manifest-v1.json"
        for slug in sorted(paper_slugs)
    ) or ("docs/papers/_safe-geometry.marker.json",)
    html_outputs = tuple(
        f"docs/papers/{slug}/index.html" for slug in sorted(paper_slugs)
    ) or ("docs/papers/_safe-html.marker.json",)
    return (
        Stage(
            "review",
            (),
            review_outputs,
            tuple("review" for _ in review_outputs),
        ),
        Stage(
            "geometry",
            (),
            geometry_outputs,
            tuple("geometry" for _ in geometry_outputs),
        ),
        Stage(
            "build-papers-index",
            (py, "pipeline/build_papers_index.py", "--topic", topic),
            ("docs/papers/_papers_index.json",),
            ("paper-index",),
        ),
        Stage(
            "classification",
            child_argv(py, "pipeline/topic_modeling.py", ["--topic", topic], policy),
            (f"{topic_root}/_new_classification.json",),
            ("classification",),
        ),
        Stage(
            "summary",
            child_argv(
                py,
                "pipeline/build_category_summaries.py",
                ["--topic", topic],
                policy,
            ),
            (f"{topic_root}/_category_summaries.json",),
            ("summary",),
        ),
        Stage(
            "connection",
            child_argv(
                py,
                "pipeline/extract_insights.py",
                ["--topic", topic, "--only", "connections"],
                policy,
            ),
            (f"{topic_root}/_paper_connections.json",),
            ("connection",),
        ),
        Stage(
            "timeline",
            child_argv(
                py,
                "pipeline/generate_timelines.py",
                ["--topic", topic, "--images", "skip"],
                policy,
            ),
            (
                f"{topic_root}/_category_narratives.json",
                f"{topic_root}/_timeline_narrative.json",
            ),
            ("timeline-categories", "timeline"),
        ),
        Stage(
            "html",
            (py, "pipeline/review_to_html.py", "--topic", topic, "--all"),
            html_outputs,
            tuple("html" for _ in html_outputs),
        ),
        Stage(
            "bm25",
            (
                py,
                "pipeline/build_search_index.py",
                "--topic",
                topic,
                "--mode",
                "bm25",
            ),
            (f"{topic_root}/_search_index.json",),
            ("bm25",),
        ),
        Stage(
            "topic-index",
            (py, "pipeline/build_topic_index.py", topic),
            (f"{topic_root}/index.html",),
            ("topic-index",),
        ),
        Stage(
            "rss",
            (py, "pipeline/build_rss.py", topic),
            (f"{topic_root}/feed.xml",),
            ("rss",),
        ),
        Stage(
            "moc",
            (py, "pipeline/generate_moc.py", "--topic", topic),
            (
                f"{topic_root}/MOC_Insights.md",
                f"{topic_root}/MOC_Categories.md",
            ),
            ("moc-insights", "moc-categories"),
        ),
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_transitional_sparse_index(
    topic: str,
    workspace_root: Path,
) -> Path:
    """Build and activate the deterministic sparse v2 index."""
    from pipeline.sparse_index import build_sparse_index

    return build_sparse_index(
        topic,
        workspace_root / "docs",
    ).active_path


def _is_sparse_v2(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    value = cast(dict[str, object], raw)
    schema = str(value.get("schema", "")).lower()
    version = value.get("schema_version")
    return version == 2 and ("bm25" in schema or "sparse" in schema)


def _artifact_rows(
    workspace: Path,
    outputs: Iterable[str],
) -> list[Mapping[str, str]]:
    rows: list[Mapping[str, str]] = []
    for relative in outputs:
        path = workspace / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"stage output missing: {relative}")
        rows.append({"path": relative, "sha256": _file_sha256(path)})
    return rows


def _resume_run_id(store: RunStateStore, topic: str) -> str | None:
    marker = store.marker_path(topic)
    if not marker.is_file():
        return None
    try:
        raw = cast(object, json.loads(marker.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("active marker must be an object")
        value = cast(dict[str, object], raw)
        run_id = value.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("active marker has no run id")
        return run_id
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        _ = store.audit_topic(topic)
        recovered = cast(
            dict[str, object],
            json.loads(marker.read_text(encoding="utf-8")),
        )
        recovery_id = recovered.get("run_id")
        raise RunStateError(
            "resume-required",
            str(recovery_id or topic),
        )


def execute_plan(
    plan: Sequence[Stage],
    *,
    topic: str,
    policy: RuntimePolicy,
    state_root: Path,
    workspace_root: Path,
    runner: Callable[[Stage], None],
    resume: bool = False,
    lease: RunLease | None = None,
) -> dict[str, object]:
    digest = policy_digest(policy)
    events: list[dict[str, str]] = []
    owned_lease = lease is None
    if lease is None:
        store = RunStateStore(state_root, workspace_root=workspace_root)
        run_id = _resume_run_id(store, topic) if resume else None
        request = RunRequest.create(
            topic,
            "safe-update",
            digest,
            run_id=run_id,
        )
        lease = store.acquire(request)
    else:
        request = lease.request
        if (
            request.topic != topic
            or request.mode != "safe-update"
            or request.policy_digest != digest
        ):
            raise RunStateError("resume-identity-mismatch", request.run_id)
    try:
        for stage in plan:
            inputs = {
                "argv_sha256": canonical_sha256(list(stage.argv)),
                "policy_sha256": digest,
            }
            if lease.stage_reusable(stage.name, inputs):
                events.append({"stage": stage.name, "status": "reused"})
                continue
            lease.set_stage_inputs(stage.name, inputs)
            runner(stage)
            artifacts = _artifact_rows(workspace_root, stage.outputs)
            lease.complete_stage(stage.name, artifacts)
            events.append({"stage": stage.name, "status": "succeeded"})
        lease.finish(RunStatus.SUCCEEDED)
    except BaseException:
        if not lease.finished:
            lease.finish(RunStatus.FAILED)
        raise
    finally:
        if owned_lease or lease.finished:
            lease.release()
    return {
        "events": events,
        "policy_sha256": digest,
        "run_id": request.run_id,
        "status": "succeeded",
    }


def acquire_plan_lease(
    topic: str,
    policy: RuntimePolicy,
    *,
    state_root: Path,
    workspace_root: Path,
    resume: bool,
) -> RunLease:
    store = RunStateStore(state_root, workspace_root=workspace_root)
    run_id = _resume_run_id(store, topic) if resume else None
    request = RunRequest.create(
        topic,
        "safe-update",
        policy_digest(policy),
        run_id=run_id,
    )
    return store.acquire(request)


def artifact_manifest(
    topic: str,
    policy: RuntimePolicy,
    workspace_root: Path,
    *,
    geometry_paths: Iterable[str],
) -> dict[str, object]:
    geometry_list = sorted(geometry_paths)
    paper_slugs = sorted(
        {
            parts[2]
            for relative in geometry_list
            for parts in [Path(relative).parts]
            if len(parts) >= 5 and parts[:2] == ("docs", "papers")
        }
    )
    plan = default_stage_plan(
        topic,
        policy,
        paper_slugs=paper_slugs,
    )
    named: list[dict[str, object]] = []
    for stage in plan:
        for name, relative in zip(stage.output_names, stage.outputs):
            path = workspace_root / relative
            exists = (
                _is_sparse_v2(path)
                if name == "bm25"
                else path.is_file() and not path.is_symlink()
            )
            named.append(
                {
                    "exists": exists,
                    "name": name,
                    "path": relative,
                    "sha256": _file_sha256(path) if exists else None,
                }
            )
    for relative in geometry_list:
        path = workspace_root / relative
        named.append(
            {
                "exists": path.is_file(),
                "name": "geometry-manifest",
                "path": relative,
                "sha256": _file_sha256(path) if path.is_file() else None,
            }
        )
    manifest: dict[str, object] = {
        "outputs": named,
        "policy_sha256": policy_digest(policy),
        "schema": "artifact-manifest-v1",
        "topic": topic,
    }
    output = workspace_root / "docs" / topic / "_artifact_manifest-v1.json"
    _atomic_write_json(output, manifest)
    return manifest


def safe_child_environment(policy: RuntimePolicy) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "ANTH" + "ROPIC_API_KEY",
        "OPEN" + "AI_API_KEY",
        "GOO" + "GLE_API_KEY",
        "GEM" + "INI_API_KEY",
        "RE" + "SEND_API_KEY",
        "CLOUD" + "FLARE_API_TOKEN",
        "CF_" + "API_TOKEN",
    ):
        _ = environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    environment[POLICY_ENV] = policy_digest(policy)
    return environment
