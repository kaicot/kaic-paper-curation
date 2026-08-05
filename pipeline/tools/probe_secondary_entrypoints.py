"""Manifest-driven zero-capability probe for quarantined entrypoints."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PIPELINE_ROOT: Final = PROJECT_ROOT / "pipeline"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.provider_inventory import load_object  # noqa: E402
from pipeline.tools.check_provider_inventory import (  # noqa: E402
    poison_import_paths,
    scan_worktree,
)


class ProbeError(RuntimeError):
    pass


def _selected_rows(
    manifest: Mapping[str, object],
    dispositions: set[str],
) -> list[dict[str, object]]:
    raw_rows = manifest.get("entrypoints")
    if not isinstance(raw_rows, list):
        raise ProbeError("manifest rows missing")
    selected: list[dict[str, object]] = []
    for item in cast(list[object], raw_rows):
        if not isinstance(item, dict):
            raise ProbeError("manifest row invalid")
        row = cast(dict[str, object], item)
        disposition = row.get("disposition")
        path = row.get("path")
        if not isinstance(disposition, str) or not isinstance(path, str):
            raise ProbeError("manifest row fields invalid")
        if disposition in {"callable", "api-compatible"}:
            raise ProbeError(f"forbidden disposition: {path}: {disposition}")
        if disposition in dispositions:
            selected.append(row)
    return sorted(selected, key=lambda row: cast(str, row["path"]))


def _probe_python(path: Path, mode: str) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    help_exit: int | None = None
    if (
        'if __name__ == "__main__"' in source
        and "/tests/" not in path.as_posix()
    ):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not (
                key.upper().endswith("_API_KEY")
                or "TOKEN" in key.upper()
                or "BASE_URL" in key.upper()
            )
        }
        environment["PATH"] = ""
        environment["PYTHONUTF8"] = "1"
        harness = "\n".join(
            (
                "import builtins,runpy,socket,subprocess,sys",
                "from pathlib import Path",
                "root=Path(sys.argv[1])",
                "sys.path[:0]=[str(root),str(root/'pipeline')]",
                "def denied(*args,**kwargs):",
                "    raise RuntimeError('capability-side-effect-poison')",
                "real_open=builtins.open",
                "def guarded_open(file,mode='r',*args,**kwargs):",
                "    if any(flag in mode for flag in ('w','a','x','+')):",
                "        denied(file,mode)",
                "    return real_open(file,mode,*args,**kwargs)",
                "builtins.open=guarded_open",
                "Path.write_bytes=denied",
                "Path.write_text=denied",
                "Path.touch=denied",
                "Path.mkdir=denied",
                "Path.unlink=denied",
                "Path.rename=denied",
                "Path.replace=denied",
                "socket.create_connection=denied",
                "socket.socket.connect=denied",
                "subprocess.Popen=denied",
                "subprocess.run=denied",
                "subprocess.call=denied",
                "subprocess.check_call=denied",
                "subprocess.check_output=denied",
                "sys.argv=[sys.argv[2],'--help']",
                "runpy.run_path(sys.argv[0],run_name='__main__')",
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", harness, str(PROJECT_ROOT), str(path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        help_exit = result.returncode
        if help_exit != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise ProbeError(
                f"help surface failed: {path.relative_to(PROJECT_ROOT)}: {detail}"
            )
    status = (
        "unavailable"
        if "SECONDARY_CAPABILITY_STATUS" in source
        else "migrated-local"
    )
    return {
        "import_exit": 0,
        "help_exit": help_exit,
        "mode": mode,
        "status": status,
    }


def _probe_javascript(path: Path, mode: str) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    marker = 'SECONDARY_CAPABILITY_STATUS = "unavailable"'
    if marker not in source:
        raise ProbeError(
            f"worker quarantine marker missing: {path.relative_to(PROJECT_ROOT)}"
        )
    return {"import_exit": 0, "mode": mode, "status": "unavailable"}


def run_probe(
    manifest_path: Path,
    dispositions: set[str],
    modes: tuple[str, ...],
) -> dict[str, object]:
    manifest = load_object(manifest_path)
    rows = _selected_rows(manifest, dispositions)
    patterns = load_object(PIPELINE_ROOT / "provider-scan-patterns-v1.json")
    attestation_candidates = (
        PROJECT_ROOT / ".omo" / "runtime" / "provider-scanner-resolved.json",
        PROJECT_ROOT.parents[1]
        / ".omo"
        / "runtime"
        / "provider-scanner-resolved.json",
    )
    attestation_path = next(
        (path for path in attestation_candidates if path.is_file()),
        None,
    )
    if attestation_path is None:
        raise ProbeError("scanner attestation unavailable")
    scanner = load_object(attestation_path)
    unexpected = [
        finding
        for finding in scan_worktree(PROJECT_ROOT, patterns, scanner)
        if finding.get("path") != "pipeline/providers/paid_compat.py"
    ]
    if unexpected:
        paths = ",".join(
            sorted(str(finding.get("path")) for finding in unexpected)
        )
        raise ProbeError(f"unresolved current entrypoints: {paths}")
    selected_paths = [cast(str, row["path"]) for row in rows]
    imported = poison_import_paths(PROJECT_ROOT, selected_paths, scanner)
    if len(imported) != len(selected_paths) or len(set(imported)) != len(imported):
        raise ProbeError(
            "poison import coverage mismatch: "
            + f"expected={len(selected_paths)};actual={len(imported)}"
        )
    results: list[dict[str, object]] = []
    for row in rows:
        relative = cast(str, row["path"])
        path = PROJECT_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ProbeError(f"entrypoint missing: {relative}")
        mode_results = [
            (
                _probe_javascript(path, mode)
                if path.suffix in {".js", ".mjs"}
                else _probe_python(path, mode)
            )
            for mode in modes
        ]
        results.append(
            {
                "disposition": row["disposition"],
                "modes": mode_results,
                "path": relative,
            }
        )
    return {
        "dispositions": sorted(dispositions),
        "modes": list(modes),
        "result": "PASS",
        "rows": results,
        "schema": "secondary-capabilities-v1",
    }


@dataclass(frozen=True, slots=True)
class Arguments:
    manifest: Path
    dispositions: str
    modes: str
    all_paths: bool
    json_out: Path


def parse_arguments(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--dispositions", required=True)
    _ = parser.add_argument("--modes", required=True)
    _ = parser.add_argument("--all", action="store_true")
    _ = parser.add_argument("--json-out", type=Path, required=True)
    namespace = parser.parse_args(argv)
    return Arguments(
        manifest=cast(Path, namespace.manifest),
        dispositions=cast(str, namespace.dispositions),
        modes=cast(str, namespace.modes),
        all_paths=cast(bool, namespace.all),
        json_out=cast(Path, namespace.json_out),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    if not arguments.all_paths:
        print("--all is required", file=sys.stderr)
        return 2
    try:
        report = run_probe(
            arguments.manifest.resolve(),
            {
                value.strip()
                for value in arguments.dispositions.split(",")
                if value.strip()
            },
            tuple(
                value.strip()
                for value in arguments.modes.split(",")
                if value.strip()
            ),
        )
        output = arguments.json_out.resolve()
        _ = output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        _ = temporary.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except (OSError, ValueError, ProbeError) as error:
        print(f"Secondary capability probe denied: {error}", file=sys.stderr)
        return 2
    rows = cast(list[dict[str, object]], report["rows"])
    print(json.dumps({"result": "PASS", "rows": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
