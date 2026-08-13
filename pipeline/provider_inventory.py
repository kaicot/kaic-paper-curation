"""Provider discovery primitives shared by the inventory CLI."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class InventoryError(RuntimeError):
    """Provider inventory input or computed state drifted."""

    detail: str

    def __str__(self) -> str:
        return self.detail


def digest_bytes(value: bytes) -> str:
    """Hash bytes with SHA-256."""
    return hashlib.sha256(value).hexdigest()


def digest(path: Path) -> str:
    """Hash one regular file with SHA-256."""
    if not path.is_file() or path.is_symlink():
        raise InventoryError(f"regular file required: {path}")
    return digest_bytes(path.read_bytes())


def load_object(path: Path) -> JsonObject:
    """Parse one JSON object."""
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryError(f"JSON object required: {path}")
    return value


def object_value(mapping: JsonObject, key: str) -> JsonObject:
    """Return one required nested JSON object."""
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise InventoryError(f"JSON object required at {key}")
    return value


def string_value(mapping: JsonObject, key: str) -> str:
    """Return one required JSON string."""
    value = mapping.get(key)
    if not isinstance(value, str):
        raise InventoryError(f"JSON string required at {key}")
    return value


def string_list(mapping: JsonObject, key: str) -> list[str]:
    """Return one required list containing only strings."""
    value = mapping.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InventoryError(f"JSON string list required at {key}")
    return [item for item in value if isinstance(item, str)]


def canonical(value: JsonValue | list[JsonObject]) -> bytes:
    """Encode canonical JSON bytes."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


class PythonProviderVisitor(ast.NodeVisitor):
    """Collect provider imports, constructors, hosts, and environment keys."""

    def __init__(self, patterns: JsonObject) -> None:
        self.modules: tuple[str, ...] = tuple(string_list(patterns, "provider_modules"))
        self.constructors: set[str] = set(string_list(patterns, "constructor_names"))
        self.environment_keys: set[str] = set(string_list(patterns, "environment_keys"))
        self.hosts: tuple[str, ...] = tuple(string_list(patterns, "provider_hosts"))
        self.provider_aliases: set[str] = set()
        self.imported_constructors: set[str] = set()
        self.reasons: set[str] = set()

    def provider_module(self, name: str) -> bool:
        """Return whether a module is inside a provider namespace."""
        return any(name == module or name.startswith(f"{module}.") for module in self.modules)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if self.provider_module(alias.name):
                self.reasons.add("python-provider-import")
                self.provider_aliases.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module is not None and self.provider_module(node.module):
            self.reasons.add("python-provider-import")
            for alias in node.names:
                self.imported_constructors.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = self.call_name(node.func)
        leaf = name.rsplit(".", 1)[-1]
        root = name.split(".", 1)[0]
        if leaf in self.constructors and (leaf in self.imported_constructors or root in self.provider_aliases):
            self.reasons.add("python-provider-constructor")
        if name in {"os.getenv", "os.environ.get"} and node.args:
            key = self.string_value(node.args[0])
            if key in self.environment_keys:
                self.reasons.add("python-provider-env-key")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if self.call_name(node.value) == "os.environ" and self.string_value(node.slice) in self.environment_keys:
            self.reasons.add("python-provider-env-key")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if isinstance(node.value, str) and any(host in node.value for host in self.hosts):
            self.reasons.add("python-provider-host")

    @staticmethod
    def call_name(node: ast.expr) -> str:
        """Return a dotted identifier for Name/Attribute expressions."""
        rendered = ast.unparse(node)
        return rendered if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", rendered) else ""

    @staticmethod
    def string_value(node: ast.expr) -> str:
        """Return a literal string or an empty sentinel."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""


def normalize(path: str) -> str:
    """Normalize one repository-relative path to a safe POSIX spelling."""
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise InventoryError(f"unsafe provider path: {path}")
    return normalized.as_posix()


def scan_source(path: str, source: bytes, patterns: JsonObject, scanner: JsonObject) -> list[str]:
    """Scan one source blob with lexical plus language-aware parsing."""
    text = source.decode("utf-8-sig")
    reasons = {
        "lexical"
        for expression in string_list(patterns, "lexical")
        if re.search(expression, text, flags=re.IGNORECASE)
    }
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        visitor = PythonProviderVisitor(patterns)
        visitor.visit(ast.parse(text, filename=path))
        reasons.update(visitor.reasons)
    elif suffix in {".js", ".mjs"}:
        reasons.update(scan_javascript(path, source, patterns, scanner))
    return sorted(reasons)


def scan_javascript(path: str, source: bytes, patterns: JsonObject, scanner: JsonObject) -> list[str]:
    """Invoke the attested parser with an exact Acorn file URL."""
    node = Path(str(scanner["node_path"]))
    parser = Path(str(scanner["parser_path"]))
    environment = {
        "SYSTEMROOT": os.environ["SYSTEMROOT"],
        "PATH": str(node.parent),
        "NODE_PATH": "",
        "TEMP": os.environ.get("TEMP", str(parser.parent)),
        "TMP": os.environ.get("TMP", str(parser.parent)),
    }
    encoded_patterns = base64.b64encode(canonical(patterns)).decode("ascii")
    result = subprocess.run(
        [
            str(node),
            str(parser),
            "--acorn-file-url",
            str(scanner["acorn_file_url"]),
            "--path",
            path,
            "--patterns-base64",
            encoded_patterns,
        ],
        input=source,
        capture_output=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise InventoryError(f"Acorn parser failed for {path}: {result.stderr.decode(errors='replace')[-500:]}")
    payload: JsonValue = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise InventoryError(f"Acorn output object required for {path}")
    return sorted(string_list(payload, "reasons"))


def source_paths(root: Path, patterns: JsonObject) -> list[str]:
    """List all current tracked or untracked sources in declared roots."""
    extensions = set(string_list(patterns, "extensions"))
    excluded = {normalize(value) for value in string_list(patterns, "excluded_paths")}
    paths: set[str] = set()
    for named_root in string_list(patterns, "roots"):
        base = root / named_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            relative = normalize(path.relative_to(root).as_posix())
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in extensions and relative not in excluded:
                paths.add(relative)
    return sorted(paths)
