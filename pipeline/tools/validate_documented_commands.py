#!/usr/bin/env python3
"""Todo 22 — validate every documented `paper-curation-command` block.

Parses fenced code blocks tagged `paper-curation-command` in the given
paths, tokenizes them with a no-shell allowlist contract, replaces
documented `<temp*>` placeholders with one unique temp root, runs each
command as `--help` or its declared common `--dry-run`, parses
`config.example.json` through the real config loader, and emits one row
`{source,line,argv,mode,exit_code,result}` per example.

Exit 0 only when every row passes and the example config resolves to the
safe defaults (llm_mode=codex, allow_paid_api=false).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ALLOWED_ENV_PREFIXES = {"PYTHONUTF8", "PAPER_CURATION_PY312"}
INTERPRETER_TOKENS = {"python", "python.exe", "python3", "py", "$py"}
SHELL_CHARS = set("|&;<>`$()*?{}[]!#")
PLACEHOLDER_RE = re.compile(r"<temp[^>]*>")
PAID_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class CommandValidationError(RuntimeError):
    """A documented command violates the no-shell token contract."""


def parse_fenced_blocks(text: str, source: str) -> list[tuple[int, str]]:
    """Return [(line_no, block_text)] for fences tagged paper-curation-command."""
    blocks: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            info = line[3:].strip()
            start = i + 1
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            if "paper-curation-command" in info:
                blocks.append((start, "\n".join(body)))
        i += 1
    return blocks


def parse_commands(block: str) -> list[str]:
    """Split one tagged block into individual commands.

    Blank lines and comment lines are skipped; bash continuation lines
    (trailing backslash) are joined into the preceding command.
    """
    commands: list[str] = []
    pending = ""
    for line in block.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        stripped = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if not stripped:
            continue
        if stripped.endswith("\\"):
            pending = (pending + " " + stripped[:-1].rstrip()).strip()
            continue
        if pending:
            commands.append(pending + " " + stripped)
            pending = ""
        else:
            commands.append(stripped)
    if pending:
        commands.append(pending)
    return [command for command in commands if command]


def tokenize_command(command: str) -> list[str]:
    """No-shell tokenization with the allowlist contract."""
    if "\\" in command:
        raise CommandValidationError("backslash path not allowed (use forward slashes)")
    if command.lstrip().startswith("&"):
        command = command.lstrip()[1:].lstrip()
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CommandValidationError(f"unbalanced quoting: {exc}") from exc
    argv: list[str] = []
    for token in tokens:
        env_match = PAID_ENV_RE.match(token)
        if env_match:
            prefix = token.split("=", 1)[0]
            if prefix not in ALLOWED_ENV_PREFIXES:
                raise CommandValidationError(f"disallowed environment assignment: {prefix}")
            continue  # applied via the child environment instead
        if token in INTERPRETER_TOKENS:
            if not argv:
                argv.append(sys.executable)
            continue
        if PLACEHOLDER_RE.search(token):
            argv.append(token)  # kept; replaced by build_argv with the temp root
            continue
        if any(ch in SHELL_CHARS for ch in token):
            raise CommandValidationError(f"shell metacharacter in token: {token}")
        argv.append(token)
    return argv


def build_argv(command: str, temp_root: Path) -> tuple[list[str], str]:
    """Return (argv, mode). Replaces <temp*> with one unique temp root."""
    argv = tokenize_command(command)
    final: list[str] = []
    for token in argv:
        if PLACEHOLDER_RE.search(token):
            final.append(PLACEHOLDER_RE.sub(lambda _match: str(temp_root), token))
        else:
            final.append(token)
    if not final:
        raise CommandValidationError("empty command")
    if final[0] != sys.executable:
        final.insert(0, sys.executable)
    mode = "dry-run" if "--dry-run" in final else "help"
    if mode == "help" and "--help" not in final:
        final.append("--help")
    return final, mode


def child_environment() -> dict[str, str]:
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
    return environment


def load_example_config(path: Path) -> dict[str, object]:
    import pipeline.config_loader as config_loader

    original = config_loader.CONFIG_PATH
    config_loader.CONFIG_PATH = path.resolve()
    config_loader._config_cache = None
    try:
        config = config_loader.load_config()
    finally:
        config_loader.CONFIG_PATH = original
        config_loader._config_cache = None
    runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
    return {
        "llm_mode": runtime.get("llm_mode"),
        "allow_paid_api": runtime.get("allow_paid_api"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="+", required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="paper-curation-docs-") as raw:
        temp_root = Path(raw)
        rows: list[dict[str, object]] = []
        failures: list[str] = []
        for path_arg in args.paths:
            path = Path(path_arg)
            if not path.is_absolute():
                path = REPO_ROOT / path
            if not path.is_file():
                failures.append(f"missing path: {path}")
                continue
            if path.name == "config.example.json":
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, block in parse_fenced_blocks(text, str(path)):
                for command in parse_commands(block):
                    try:
                        argv, mode = build_argv(command, temp_root)
                    except CommandValidationError as exc:
                        row = {
                            "source": str(path),
                            "line": line_no,
                            "argv": [],
                            "mode": "rejected",
                            "exit_code": None,
                            "result": "fail",
                            "error": str(exc),
                        }
                        rows.append(row)
                        failures.append(f"{path}:{line_no}: {exc}")
                        continue
                    try:
                        result = subprocess.run(
                            argv,
                            cwd=REPO_ROOT,
                            env=child_environment(),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            check=False,
                            timeout=180,
                        )
                        exit_code = result.returncode
                    except subprocess.TimeoutExpired:
                        exit_code = -1
                    row = {
                        "source": str(path),
                        "line": line_no,
                        "argv": argv,
                        "mode": mode,
                        "exit_code": exit_code,
                        "result": "pass" if exit_code == 0 else "fail",
                    }
                    rows.append(row)
                    if exit_code != 0:
                        failures.append(f"{path}:{line_no}: exit {exit_code}")

        config: dict[str, object] = {}
        config_path_seen = False
        for path_arg in args.paths:
            path = Path(path_arg)
            if not path.is_absolute():
                path = REPO_ROOT / path
            if path.name == "config.example.json" and path.is_file():
                config = load_example_config(path)
                config_path_seen = True

        config_ok = True
        if config_path_seen:
            config_ok = config.get("llm_mode") == "codex" and config.get("allow_paid_api") is False
            if not config_ok:
                failures.append(f"config.example.json does not resolve safe defaults: {config}")

        payload = {
            "schema": "docs-validation-v1",
            "schema_version": 1,
            "rows": rows,
            "config": config,
            "config_safe_defaults": config_ok,
            "result": "PASS" if not failures else "FAIL",
            "failures": failures,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write(text + "\n")
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text + "\n", encoding="utf-8")
        return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
