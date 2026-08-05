"""Read-only readiness checks for the local Codex paper-curation stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, final


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = PROJECT_ROOT / "pipeline"
for candidate in (PROJECT_ROOT, PIPELINE_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pipeline.config_loader import (  # noqa: E402
    local_zotero_status,
    resolve_user_profile,
)
from pipeline.lib.specter2_cache import (  # noqa: E402
    Specter2CacheUnavailable,
    verify_cache,
)
from pipeline.providers.codex_gateway import (  # noqa: E402
    CodexGateway,
    CodexGatewayError,
)
from pipeline.query_search_index import (  # noqa: E402
    SparseQueryError,
    query_search_index,
)
from pipeline.runtime_policy import (  # noqa: E402
    JsonObject,
    PAID_CAPABILITIES,
    RuntimePolicy,
    RuntimePolicyError,
    resolve_runtime_policy,
)


CheckStatus = Literal["pass", "fail", "warn", "skipped"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: CheckStatus
    code: str


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    required: bool
    status: CheckStatus
    code: str

    def json_value(self) -> dict[str, bool | str]:
        return {
            "code": self.code,
            "id": self.id,
            "required": self.required,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]
    exit_code: int
    mode: str
    status: Literal["ready", "not-ready", "error"]

    def json_value(self) -> dict[str, object]:
        return {
            "checks": [check.json_value() for check in self.checks],
            "mode": self.mode,
            "schema": "doctor-report-v1",
            "schema_version": 1,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class DoctorDependencies:
    python_runtime: Callable[[], CheckResult]
    policy: Callable[[], CheckResult]
    codex_attestation: Callable[[], CheckResult]
    codex_login: Callable[[], CheckResult]
    codex_canary: Callable[[], CheckResult]
    disabled_features: Callable[[], CheckResult]
    zotero: Callable[[], CheckResult]
    bm25: Callable[[str | None], CheckResult]
    geometry: Callable[[str | None], CheckResult]
    loopback: Callable[[], CheckResult]
    specter2: Callable[[], CheckResult]

    @classmethod
    def for_testing(
        cls,
        *,
        python_runtime: Callable[[], CheckResult],
        policy: Callable[[], CheckResult],
        codex_attestation: Callable[[], CheckResult],
        codex_login: Callable[[], CheckResult],
        codex_canary: Callable[[], CheckResult],
        disabled_features: Callable[[], CheckResult],
        zotero: Callable[[], CheckResult],
        bm25: Callable[[str | None], CheckResult],
        geometry: Callable[[str | None], CheckResult],
        loopback: Callable[[], CheckResult],
        specter2: Callable[[], CheckResult],
    ) -> "DoctorDependencies":
        return cls(
            python_runtime,
            policy,
            codex_attestation,
            codex_login,
            codex_canary,
            disabled_features,
            zotero,
            bm25,
            geometry,
            loopback,
            specter2,
        )


CHECKS: tuple[tuple[str, bool], ...] = (
    ("python-runtime", True),
    ("runtime-policy", True),
    ("codex-attestation", True),
    ("codex-login", True),
    ("codex-canary", False),
    ("disabled-features", True),
    ("zotero-local", True),
    ("bm25", False),
    ("geometry", False),
    ("loopback", True),
    ("specter2-cache", False),
)


def _skipped(
    rows: list[DoctorCheck],
    start: int,
    *,
    canary_required: bool,
) -> None:
    for check_id, required in CHECKS[start:]:
        rows.append(
            DoctorCheck(
                check_id,
                canary_required if check_id == "codex-canary" else required,
                "skipped",
                "precondition-failed",
            )
        )


def run_doctor(
    dependencies: DoctorDependencies,
    *,
    mode: Literal["codex", "off"],
    topic: str | None,
    codex_canary: bool,
) -> DoctorReport:
    rows: list[DoctorCheck] = []

    def add(
        check_id: str,
        required: bool,
        function: Callable[[], CheckResult],
    ) -> CheckResult:
        result = function()
        rows.append(
            DoctorCheck(
                check_id,
                required,
                result.status,
                result.code,
            )
        )
        return result

    python = add("python-runtime", True, dependencies.python_runtime)
    if python.status != "pass":
        _skipped(rows, 1, canary_required=codex_canary)
        return DoctorReport(tuple(rows), 2, "readiness", "error")
    policy = add("runtime-policy", True, dependencies.policy)
    if policy.status != "pass":
        _skipped(rows, 2, canary_required=codex_canary)
        return DoctorReport(tuple(rows), 2, "readiness", "error")

    if mode == "codex":
        attestation = add(
            "codex-attestation",
            True,
            dependencies.codex_attestation,
        )
        if attestation.status != "pass":
            _skipped(rows, 3, canary_required=codex_canary)
            return DoctorReport(
                tuple(rows),
                1,
                "readiness",
                "not-ready",
            )
        login = add("codex-login", True, dependencies.codex_login)
        if login.status != "pass":
            _skipped(rows, 4, canary_required=codex_canary)
            return DoctorReport(
                tuple(rows),
                1,
                "readiness",
                "not-ready",
            )
        if codex_canary:
            canary = add(
                "codex-canary",
                True,
                dependencies.codex_canary,
            )
            if canary.status != "pass":
                _skipped(rows, 5, canary_required=True)
                return DoctorReport(
                    tuple(rows),
                    1,
                    "canary",
                    "not-ready",
                )
        else:
            rows.append(
                DoctorCheck(
                    "codex-canary",
                    False,
                    "skipped",
                    "not-requested",
                )
            )
    else:
        rows.extend(
            DoctorCheck(
                check_id,
                False,
                "skipped",
                "runtime-off",
            )
            for check_id in (
                "codex-attestation",
                "codex-login",
                "codex-canary",
            )
        )

    _ = add("disabled-features", True, dependencies.disabled_features)
    _ = add("zotero-local", True, dependencies.zotero)
    _ = add(
        "bm25",
        topic is not None,
        lambda: dependencies.bm25(topic),
    )
    _ = add(
        "geometry",
        topic is not None,
        lambda: dependencies.geometry(topic),
    )
    _ = add("loopback", True, dependencies.loopback)
    _ = add("specter2-cache", False, dependencies.specter2)
    failed = any(
        row.required and row.status != "pass"
        for row in rows
        if row.status != "skipped"
    )
    return DoctorReport(
        tuple(rows),
        1 if failed else 0,
        "canary" if codex_canary else "readiness",
        "not-ready" if failed else "ready",
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _runtime_root() -> Path:
    for parent in (PROJECT_ROOT, *PROJECT_ROOT.parents):
        if (
            (parent / ".tools/python312/python.exe").is_file()
            and (parent / ".omo/runtime/python312-resolved.json").is_file()
        ):
            return parent
    raise RuntimeError("python-runtime-unavailable")


def _python_status() -> CheckResult:
    try:
        root = _runtime_root()
        runtime = root / ".tools/python312"
        executable = runtime / "python.exe"
        attestation_path = root / ".omo/runtime/python312-resolved.json"
        attestation_value = cast(
            object,
            json.loads(attestation_path.read_text(encoding="utf-8")),
        )
        if not isinstance(attestation_value, dict):
            raise ValueError
        attestation = cast(dict[str, object], attestation_value)
        if (
            tuple(sys.version_info[:3]) != (3, 12, 10)
            or os.path.normcase(str(Path(sys.executable).resolve()))
            != os.path.normcase(str(executable.resolve()))
            or _digest(executable)
            != attestation.get("python_executable_sha256")
            or _digest(runtime / "python312._pth")
            != attestation.get("pth_sha256")
            or _digest(runtime / "python312.zip")
            != attestation.get("stdlib_sha256")
        ):
            raise ValueError
        raw_package_files = attestation.get("package_files")
        if not isinstance(raw_package_files, list):
            raise ValueError
        package_files = cast(list[object], raw_package_files)
        for item in package_files:
            if not isinstance(item, dict):
                raise ValueError
            package = cast(dict[str, object], item)
            relative = package.get("path")
            if not isinstance(relative, str):
                raise ValueError
            candidate = runtime / relative
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != package.get("size")
                or _digest(candidate) != package.get("sha256")
            ):
                raise ValueError
        return CheckResult("pass", "python-runtime-attested")
    except Exception:
        return CheckResult("fail", "python-runtime-invalid")


def _load_config(path: Path) -> JsonObject:
    def pairs(value: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in value:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    value = cast(
        object,
        json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        ),
    )
    if not isinstance(value, dict):
        raise ValueError
    return cast(JsonObject, value)


@final
class ProductionDoctor:
    project_root: Path
    config_path: Path
    docs_dir: Path
    mode: Literal["codex", "off"]
    profile: Path

    def __init__(
        self,
        project_root: Path,
        config_path: Path,
        docs_dir: Path,
        mode: Literal["codex", "off"],
        profile: Path,
    ) -> None:
        self.project_root = project_root
        self.config_path = config_path
        self.docs_dir = docs_dir
        self.mode = mode
        self.profile = profile
        self._config: JsonObject | None = None
        self._policy: RuntimePolicy | None = None
        self._gateway: CodexGateway | None = None

    def config(self) -> JsonObject:
        if self._config is None:
            self._config = _load_config(self.config_path)
        return self._config

    def policy(self) -> CheckResult:
        try:
            self._policy = resolve_runtime_policy(
                self.config(),
                self.mode,
            )
            return CheckResult("pass", "runtime-policy-valid")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return CheckResult("fail", "runtime-policy-invalid")

    def gateway(self) -> CodexGateway:
        if self._gateway is None:
            self._gateway = CodexGateway.production(self.project_root)
        return self._gateway

    def codex_attestation(self) -> CheckResult:
        try:
            _ = self.gateway().capability_inventory()
            return CheckResult("pass", "codex-attested")
        except CodexGatewayError:
            return CheckResult("fail", "codex-attestation-invalid")

    def codex_login(self) -> CheckResult:
        marker = self.profile / ".codex" / "auth.json"
        if not marker.is_file() or marker.is_symlink():
            return CheckResult("fail", "codex-login-missing")
        try:
            _ = self.gateway().preflight()
            return CheckResult("pass", "codex-login-valid")
        except CodexGatewayError:
            return CheckResult("fail", "codex-login-invalid")

    def codex_canary(self) -> CheckResult:
        try:
            _ = self.gateway().requalify(accept=False)
            return CheckResult("pass", "codex-canary-valid")
        except CodexGatewayError:
            return CheckResult("fail", "codex-canary-failed")

    def disabled_features(self) -> CheckResult:
        policy = self._policy
        if policy is None:
            return CheckResult("fail", "runtime-policy-missing")
        envelope = policy.envelope()
        raw = envelope.get("capabilities")
        if not isinstance(raw, dict):
            return CheckResult("fail", "disabled-feature-drift")
        for name in PAID_CAPABILITIES:
            capability = raw.get(name)
            if (
                not isinstance(capability, dict)
                or capability.get("allowed") is not False
            ):
                return CheckResult("fail", "disabled-feature-drift")
        return CheckResult("pass", "disabled-features-enforced")

    def zotero(self) -> CheckResult:
        try:
            status = local_zotero_status(self.config())
        except Exception:
            return CheckResult("fail", "zotero-config-invalid")
        required = (
            status["api_key_configured"],
            status["email_configured"],
            status["pdf_dir_configured"],
            status["pdf_dir_exists"],
            status["user_id_configured"],
            int(status["collection_count"]) > 0,
        )
        return CheckResult(
            "pass" if all(required) else "fail",
            "zotero-local-ready"
            if all(required)
            else "zotero-local-incomplete",
        )

    def bm25(self, topic: str | None) -> CheckResult:
        if topic is None:
            return CheckResult("warn", "topic-not-selected")
        try:
            result = query_search_index(
                topic=topic,
                query="health",
                top_k=1,
                mode="bm25",
                docs_dir=self.docs_dir,
            )
            return CheckResult(
                "pass" if result.get("status") == "ok" else "fail",
                "bm25-ready"
                if result.get("status") == "ok"
                else "bm25-not-ready",
            )
        except SparseQueryError:
            return CheckResult("fail", "bm25-not-ready")

    def geometry(self, topic: str | None) -> CheckResult:
        if topic is None:
            return CheckResult("warn", "topic-not-selected")
        try:
            index_path = self.docs_dir / topic / "_search_index.json"
            if index_path.is_symlink() or not index_path.is_file():
                raise ValueError
            index_raw = cast(
                object,
                json.loads(index_path.read_text(encoding="utf-8")),
            )
            if not isinstance(index_raw, dict):
                raise ValueError
            raw_documents = cast(dict[str, object], index_raw).get(
                "documents"
            )
            if not isinstance(raw_documents, list):
                raise ValueError
            documents = cast(list[object], raw_documents)
            papers_root = (self.docs_dir / "papers").resolve()
            for document_raw in documents:
                if not isinstance(document_raw, dict):
                    raise ValueError
                slug_raw = cast(dict[str, object], document_raw).get("slug")
                if (
                    not isinstance(slug_raw, str)
                    or not slug_raw
                    or Path(slug_raw).name != slug_raw
                    or slug_raw in (".", "..")
                ):
                    raise ValueError
                paper_candidate = papers_root / slug_raw
                if (
                    paper_candidate.is_symlink()
                    or (
                        paper_candidate.exists()
                        and paper_candidate.is_junction()
                    )
                ):
                    raise ValueError
                paper_dir = paper_candidate.resolve()
                _ = paper_dir.relative_to(papers_root)
                manifest_path = paper_dir / "figures/manifest-v1.json"
                if (
                    manifest_path.is_symlink()
                    or not manifest_path.is_file()
                ):
                    raise ValueError
                manifest_raw = cast(
                    object,
                    json.loads(manifest_path.read_text(encoding="utf-8")),
                )
                if not isinstance(manifest_raw, dict):
                    raise ValueError
                manifest = cast(dict[str, object], manifest_raw)
                source_hash = manifest.get("source_pdf_sha256")
                raw_rows = manifest.get("rows")
                if (
                    manifest.get("schema") != "geometry-figures-v1"
                    or not isinstance(source_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
                    or not isinstance(raw_rows, list)
                ):
                    raise ValueError
                rows = cast(list[object], raw_rows)
                for row_raw in rows:
                    if not isinstance(row_raw, dict):
                        raise ValueError
                    row = cast(dict[str, object], row_raw)
                    relative = row.get("path")
                    page = row.get("page")
                    caption = row.get("caption")
                    image_hash = row.get("sha256")
                    if (
                        not isinstance(relative, str)
                        or re.fullmatch(
                            r"figures/fig[0-9]+[.]png",
                            relative,
                        )
                        is None
                        or not isinstance(page, int)
                        or isinstance(page, bool)
                        or page < 0
                        or not isinstance(caption, str)
                        or not isinstance(image_hash, str)
                        or re.fullmatch(r"[0-9a-f]{64}", image_hash)
                        is None
                    ):
                        raise ValueError
                    image_candidate = paper_dir / relative
                    if image_candidate.is_symlink():
                        raise ValueError
                    image = image_candidate.resolve()
                    _ = image.relative_to(paper_dir)
                    if (
                        not image.is_file()
                        or _digest(image) != image_hash
                    ):
                        raise ValueError
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return CheckResult("fail", "geometry-invalid")
        return CheckResult("pass", "geometry-ready")

    def loopback(self) -> CheckResult:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            host = cast(tuple[str, int], listener.getsockname())[0]
            return CheckResult(
                "pass" if host == "127.0.0.1" else "fail",
                "loopback-ready"
                if host == "127.0.0.1"
                else "loopback-invalid",
            )
        except OSError:
            return CheckResult("fail", "loopback-unavailable")
        finally:
            listener.close()

    def specter2(self) -> CheckResult:
        try:
            _ = verify_cache(
                self.project_root / ".cache",
                verify_files=False,
            )
            return CheckResult("pass", "specter2-cache-ready")
        except Specter2CacheUnavailable:
            return CheckResult("warn", "specter2-cache-not-ready")

    def dependencies(self) -> DoctorDependencies:
        return DoctorDependencies(
            _python_status,
            self.policy,
            self.codex_attestation,
            self.codex_login,
            self.codex_canary,
            self.disabled_features,
            self.zotero,
            self.bm25,
            self.geometry,
            self.loopback,
            self.specter2,
        )


@dataclass(frozen=True, slots=True)
class Arguments:
    codex_canary: bool
    config: Path
    docs_dir: Path
    format: Literal["json", "text"]
    llm_mode: Literal["codex", "off"] | None
    topic: str | None


def _arguments(argv: list[str] | None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--codex-canary", action="store_true")
    _ = parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.json",
    )
    _ = parser.add_argument(
        "--docs-dir",
        type=Path,
        default=PROJECT_ROOT / "docs",
    )
    _ = parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
    )
    _ = parser.add_argument(
        "--llm-mode",
        choices=("codex", "off"),
    )
    _ = parser.add_argument("--topic")
    namespace = parser.parse_args(argv)
    mode = cast(Literal["codex", "off"] | None, namespace.llm_mode)
    canary = cast(bool, namespace.codex_canary)
    if canary and mode == "off":
        parser.error("--codex-canary requires Codex mode")
    return Arguments(
        canary,
        cast(Path, namespace.config),
        cast(Path, namespace.docs_dir),
        cast(Literal["json", "text"], namespace.format),
        mode,
        cast(str | None, namespace.topic),
    )


def _emit(report: DoctorReport, output_format: str) -> None:
    if output_format == "json":
        print(
            json.dumps(
                report.json_value(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    print(f"Doctor: {report.status}")
    for check in report.checks:
        print(f"[{check.status.upper()}] {check.id}: {check.code}")


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        profile = resolve_user_profile()
        config = _load_config(arguments.config)
        policy = resolve_runtime_policy(
            config,
            arguments.llm_mode,
        )
        production = ProductionDoctor(
            PROJECT_ROOT,
            arguments.config,
            arguments.docs_dir,
            policy.mode,
            profile,
        )
        report = run_doctor(
            production.dependencies(),
            mode=policy.mode,
            topic=arguments.topic,
            codex_canary=arguments.codex_canary,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimePolicyError,
        ValueError,
    ):
        report = DoctorReport((), 2, "readiness", "error")
    _emit(report, arguments.format)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
