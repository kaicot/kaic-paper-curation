"""Route entry points to the verified project-local CPython 3.12 runtime.

An ambient Python 3.12 is only a *qualification candidate*.  Ordinary runs
always use the published last-known-good runtime, so a broken ``py`` launcher
or a system/uv Python upgrade cannot silently change pipeline behavior.
"""

import os
import subprocess
import sys
from pathlib import Path

try:  # ``run_citedby.py`` may be launched as a file from ``pipeline/``.
    from pipeline.python_runtime import PythonRuntimeError, discover_candidates, resolve_runtime
except ModuleNotFoundError:  # pragma: no cover - exercised by direct entrypoints
    from python_runtime import PythonRuntimeError, discover_candidates, resolve_runtime


def find_py312() -> str | None:
    """Return the verified project runtime, never an unqualified candidate."""
    try:
        return str(resolve_runtime().executable)
    except PythonRuntimeError:
        return None


def qualification_candidates() -> tuple[str, ...]:
    """Expose discovered candidates for an explicit bootstrap qualification."""
    try:
        return tuple(str(path) for path in discover_candidates())
    except PythonRuntimeError:
        return ()


def force_py312() -> None:
    """Re-execute under the attested project runtime or fail before work starts."""
    try:
        runtime = resolve_runtime()
    except PythonRuntimeError as error:
        raise SystemExit(
            "paper-curation 의 프로젝트 Python 3.12 런타임을 검증할 수 없습니다: "
            f"{error}. bootstrap_python_runtime.py --check-only 로 확인하세요."
        ) from error
    current = os.path.normcase(str(Path(sys.executable).resolve()))
    expected = os.path.normcase(str(runtime.executable.resolve()))
    if current == expected:
        return
    if os.environ.get("_PC_PY312_REEXEC") == "1":
        raise SystemExit(
            "paper-curation 이 검증된 프로젝트 Python 3.12 런타임으로 재실행되지 않았습니다: "
            f"{sys.executable}"
        )
    os.environ["_PC_PY312_REEXEC"] = "1"
    # Keep this forwarding diagnostic ASCII: callers frequently capture it
    # with UTF-8 even when the parent Windows console still uses a legacy page.
    print(f"[env] project Python 3.12 runtime: {sys.executable} -> {runtime.executable}", file=sys.stderr)
    if os.name == "nt":
        # ``os.execv`` on Windows replaces only the low-level process image;
        # callers can observe the parent process's success code instead of the
        # child's policy/lock failure.  Forward the child status explicitly.
        child_environment = os.environ.copy()
        child_environment.setdefault("PYTHONUTF8", "1")
        child_environment.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(
            [str(runtime.executable), *sys.argv],
            env=child_environment,
            check=False,
        )
        raise SystemExit(result.returncode)
    os.execv(str(runtime.executable), [str(runtime.executable), *sys.argv])
