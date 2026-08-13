"""Safe single entrypoint for the local paper-curation pipeline."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

PIPELINE = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config_loader import DOCS_DIR, load_config  # noqa: E402
from pipeline.release_dry_run import (  # noqa: E402
    ArtifactValidationError,
    build_dry_run_plan,
    build_policy_denied_result,
    canonical_json,
    emit,
    validate_default_artifacts,
)
from pipeline.runtime_policy import (  # noqa: E402
    JsonObject,
    RuntimePolicy,
    RuntimePolicyError,
    RuntimeMode,
    resolve_runtime_policy,
)
from pipeline.update_geometry_orchestration import (  # noqa: E402
    approved_python,
    safe_child_environment,
)


class Runner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> int: ...


@dataclass
class RunOptions:
    topic: str = ""
    llm_mode: str | None = None
    mode: str = "curate"
    source: str = "zotero"
    fixture: Path | None = None
    images: str = "skip"
    with_search: bool = False
    no_search: bool = False
    with_register: bool = False
    no_register: bool = False
    with_sync: bool = False
    no_sync: bool = False
    days: int = 7
    max_papers: int = 20
    concurrency: int = 1
    slugs: str = ""
    strict_pdf: bool = False
    also_reclassify: bool = False
    insights: bool = False
    local_fallback: bool = False
    conn_full: bool = False
    skip_dedup: bool = False
    skip_metrics: bool = False
    dedup_execute: bool = False
    dry_run: bool = False
    yes: bool = False
    no_validate: bool = False
    docs_dir: Path = DOCS_DIR


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run(
    command: Sequence[str],
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Execute one controlled child and return its process exit code."""
    return subprocess.call(
        [str(item) for item in command],
        timeout=timeout,
        env=env,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local-only paper-curation orchestration",
    )
    _ = parser.add_argument("--topic", required=True)
    _ = parser.add_argument("--llm-mode", choices=["codex", "off"], default=None)
    _ = parser.add_argument(
        "--mode",
        choices=[
            "curate",
            "rebuild",
            "reclassify",
            "retime",
            "audit",
            "fix-matching",
            "dedup",
            "validate",
        ],
        default="curate",
    )
    _ = parser.add_argument(
        "--source",
        choices=["web", "zotero", "fixture"],
        default="zotero",
    )
    _ = parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="tracked one-paper fixture directory (requires --source fixture)",
    )
    _ = parser.add_argument(
        "--images",
        choices=["skip", "changed", "all"],
        default="skip",
    )
    _ = parser.add_argument("--with-search", action="store_true")
    _ = parser.add_argument("--no-search", action="store_true")
    _ = parser.add_argument("--with-register", action="store_true")
    _ = parser.add_argument("--no-register", action="store_true")
    _ = parser.add_argument("--with-sync", action="store_true")
    _ = parser.add_argument("--no-sync", action="store_true")
    _ = parser.add_argument("--days", type=_positive_integer, default=7)
    _ = parser.add_argument("--max-papers", type=_positive_integer, default=20)
    _ = parser.add_argument("--concurrency", type=_positive_integer, default=1)
    _ = parser.add_argument("--slugs", default="")
    _ = parser.add_argument("--strict-pdf", action="store_true")
    _ = parser.add_argument("--also-reclassify", action="store_true")
    _ = parser.add_argument("--insights", action="store_true")
    _ = parser.add_argument("--local-fallback", action="store_true")
    _ = parser.add_argument("--conn-full", action="store_true")
    _ = parser.add_argument("--skip-dedup", action="store_true")
    _ = parser.add_argument("--skip-metrics", action="store_true")
    _ = parser.add_argument("--dedup-execute", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--yes", action="store_true")
    _ = parser.add_argument("--no-validate", action="store_true")
    _ = parser.add_argument(
        "--docs-dir",
        type=Path,
        default=DOCS_DIR,
        help=argparse.SUPPRESS,
    )
    return parser


def resolve_source_routing(args: RunOptions) -> dict[str, bool]:
    if args.source == "fixture":
        return {"search": False, "register": False, "sync": False}
    routing = (
        {"search": True, "register": True, "sync": True}
        if args.source == "web"
        else {"search": False, "register": False, "sync": True}
    )
    if args.with_search:
        routing["search"] = True
    if args.no_search:
        routing["search"] = False
    if args.with_register:
        routing["register"] = True
    if args.no_register:
        routing["register"] = False
    if args.with_sync:
        routing["sync"] = True
    if args.no_sync:
        routing["sync"] = False
    if args.mode in {"reclassify", "retime"} and not any(
        (args.with_search, args.with_register, args.with_sync)
    ):
        routing = {"search": False, "register": False, "sync": False}
    return routing


def resolve_images_default(args: RunOptions) -> str:
    return str(args.images)


def _policy(args: RunOptions) -> RuntimePolicy:
    if args.llm_mode is not None:
        return resolve_runtime_policy(
            {
                "runtime": {
                    "allow_paid_api": False,
                    "llm_mode": args.llm_mode,
                },
                "schema_version": 2,
            },
            args.llm_mode,
        )
    return resolve_runtime_policy(cast(JsonObject, load_config()))


def build_update_force_cmd(
    args: RunOptions,
    images: str,
    *,
    python: str | Path | None = None,
    policy: RuntimePolicy | None = None,
) -> list[str]:
    selected_python = python or approved_python()
    selected_policy = policy or RuntimePolicy(
        cast(RuntimeMode, args.llm_mode or "codex")
    )
    command = [
        str(selected_python),
        "-u",
        str(PIPELINE / "run_update_force.py"),
        "--topic",
        args.topic,
        "--mode",
        args.mode,
        "--concurrency",
        str(args.concurrency),
        "--llm-mode",
        selected_policy.mode,
        "--skip-dedup",
    ]
    if args.strict_pdf:
        command.append("--strict-pdf")
    if args.slugs:
        command.extend(["--slugs", args.slugs])
    if args.also_reclassify:
        command.append("--category")
    if args.conn_full:
        command.append("--conn-full")
    if images == "changed":
        command.append("--ensure-timeline")
    elif images == "all":
        command.append("--timeline")
    return command


def build_tool_plan(
    args: RunOptions,
    *,
    python: str | Path | None = None,
) -> list[list[str]] | None:
    selected_python = str(python or approved_python())
    prefix = [selected_python, "-u"]
    if args.mode == "audit":
        return [
            prefix
            + [str(PIPELINE / "audit_matching.py"), "--topic", args.topic]
        ]
    if args.mode == "fix-matching":
        return [
            prefix
            + [str(PIPELINE / "fix_matching.py"), "--topic", args.topic]
        ]
    if args.mode == "dedup":
        return [
            prefix
            + [str(PIPELINE / "dedup_zotero.py"), "--topic", args.topic]
        ]
    if args.mode == "validate":
        return [
            prefix
            + [
                str(PIPELINE / "validate_papers.py"),
                "--topic",
                args.topic,
                "--strict",
            ]
        ]
    return None


def _pipeline_plan(
    args: RunOptions,
    policy: RuntimePolicy,
    python: str | Path,
) -> list[list[str]]:
    if args.source == "fixture":
        if not args.fixture:
            print("fixture-path-required", file=sys.stderr)
            raise SystemExit(2)
        return [
            [
                str(python),
                "-u",
                str(PIPELINE / "tools/run_fixture_curation.py"),
                "--topic",
                args.topic,
                "--fixture",
                str(args.fixture),
                "--docs-root",
                str(args.docs_dir.resolve()),
            ]
        ]
    routing = resolve_source_routing(args)
    prefix = [str(python), "-u"]
    commands: list[list[str]] = []
    if routing["search"]:
        commands.append(
            prefix
            + [
                str(PIPELINE / "search_papers.py"),
                "--topic",
                args.topic,
                "--days",
                str(args.days),
                "--max-papers",
                str(args.max_papers),
            ]
        )
    if routing["register"]:
        commands.append(
            prefix
            + [str(PIPELINE / "register_zotero.py"), "--topic", args.topic]
        )
    if routing["sync"]:
        commands.append(
            prefix
            + [str(PIPELINE / "sync_zotero.py"), "--topic", args.topic]
        )
    commands.append(
        build_update_force_cmd(
            args,
            resolve_images_default(args),
            python=python,
            policy=policy,
        )
    )
    return commands


def _dry_run(args: RunOptions) -> int:
    policy_mode = args.llm_mode or "codex"
    emit(
        build_dry_run_plan(
            entrypoint="pipeline/run_full.py",
            topic=args.topic,
            mode=args.mode,
            source=args.source,
            images=args.images,
            concurrency=args.concurrency,
            policy_mode=policy_mode,
        )
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
) -> int:
    args = build_parser().parse_args(argv, namespace=RunOptions())
    if args.dry_run:
        return _dry_run(args)
    if args.concurrency != 1:
        print("safe-concurrency-required", file=sys.stderr)
        return 2
    if args.images != "skip":
        print("image-generation-disabled", file=sys.stderr)
        return 2
    if args.insights or args.local_fallback or args.dedup_execute:
        print("unsupported-safe-profile-option", file=sys.stderr)
        return 2
    try:
        policy = _policy(args)
    except RuntimePolicyError as error:
        print(error.code, file=sys.stderr)
        return 2
    docs_dir = args.docs_dir.resolve()
    if policy.mode == "off":
        emit(build_policy_denied_result(args.topic, docs_dir))
        return 3

    selected_python = approved_python()
    selected_runner = runner or run
    tools = build_tool_plan(args, python=selected_python)
    commands = tools if tools is not None else _pipeline_plan(
        args,
        policy,
        selected_python,
    )
    environment = safe_child_environment(policy)
    try:
        for command in commands:
            try:
                code = selected_runner(
                    command,
                    timeout=None,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                code = 124
            if code != 0:
                return int(code)
        if tools is None and args.mode == "curate" and args.source != "fixture":
            try:
                validators = validate_default_artifacts(
                    args.topic,
                    docs_dir,
                )
            except ArtifactValidationError as error:
                print(
                    canonical_json(
                        {
                            "code": error.code,
                            "schema": "run-result-v1",
                            "schema_version": 1,
                            "stage": error.stage,
                            "status": "artifact_invalid",
                            "topic": args.topic,
                        }
                    ),
                    end="",
                    file=sys.stderr,
                )
                return 1
            emit(
                {
                    "policy": policy.config_value(),
                    "schema": "run-result-v1",
                    "schema_version": 1,
                    "status": "succeeded",
                    "topic": args.topic,
                    "validators": validators,
                }
            )
        return 0
    finally:
        pass


if __name__ == "__main__":
    from pipeline._env_guard import force_py312

    force_py312()
    raise SystemExit(main())
