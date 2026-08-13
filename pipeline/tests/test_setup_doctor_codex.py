"""Todo 20 local-only setup and Doctor acceptance tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "pipeline"
for candidate in (ROOT, PIPELINE):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pipeline import config_loader  # noqa: E402
from pipeline import doctor  # noqa: E402
from pipeline import setup  # noqa: E402
from pipeline.providers.codex_gateway import (  # noqa: E402
    CodexGateway,
    GatewayPaths,
)
from pipeline.tests.test_codex_exec import FakeRunner  # noqa: E402


TOPIC = "qa_fixture"


@final
class FakeResponse:
    payload: bytes

    def __init__(self, value: object) -> None:
        self.payload = json.dumps(value).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


@final
class FakePreflightGateway:
    def preflight(self) -> dict[str, bool]:
        return {"attested": True}


def local_config(pdf_dir: Path) -> dict[str, object]:
    return {
        "runtime": {
            "allow_paid_api": False,
            "llm_mode": "codex",
            "schema_version": 2,
        },
        "schema_version": 2,
        "unpaywall_email": "qa@example.test",
        "zotero": {
            "api_key": "zotero-fixture-secret",
            "collections": {TOPIC: "ABCD1234"},
            "email": "qa@example.test",
            "pdf_dir": str(pdf_dir),
            "user_id": "12345",
        },
    }


@final
class SetupDoctorCodexTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] | None = None
    root = Path()
    profile = Path()
    config_path = Path()
    pdf_dir = Path()

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="setup-doctor-")
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.pdf_dir = self.root / "zotero-pdfs"
        self.pdf_dir.mkdir()
        self.config_path = self.root / "config.json"

    @override
    def tearDown(self) -> None:
        assert self.temporary is not None
        self.temporary.cleanup()

    def profile_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.profile),
                "HOMEDRIVE": self.profile.drive or "C:",
                "HOMEPATH": str(self.profile)[2:]
                if self.profile.drive
                else str(self.profile),
                "PYTHONUTF8": "1",
                "USERPROFILE": str(self.profile),
            }
        )
        return environment

    def run_setup(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PIPELINE / "setup.py"), *arguments],
            cwd=ROOT,
            env=environment or self.profile_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def run_doctor_cli(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PIPELINE / "doctor.py"), *arguments],
            cwd=ROOT,
            env=environment or self.profile_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_default_setup_is_closed_input_and_repo_local(self) -> None:
        _ = self.config_path.write_text(
            json.dumps(local_config(self.pdf_dir)),
            encoding="utf-8",
        )
        before = sorted(path.relative_to(self.profile) for path in self.profile.rglob("*"))

        result = self.run_setup(
            "--config",
            str(self.config_path),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload_value = cast(object, json.loads(result.stdout))
        self.assertIsInstance(payload_value, dict)
        payload = cast(dict[str, object], payload_value)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(
            sorted(path.relative_to(self.profile) for path in self.profile.rglob("*")),
            before,
        )
        self.assertFalse((self.profile / ".codex").exists())
        self.assertNotIn("PaperBanana", result.stdout)
        self.assertNotIn("deploy", result.stdout.lower())
        self.assertNotIn("curation", result.stdout.lower())

    def test_default_setup_missing_input_fails_without_prompt_or_write(self) -> None:
        result = self.run_setup(
            "--config",
            str(self.config_path),
            "--json",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "input-required")
        self.assertEqual(list(self.profile.iterdir()), [])
        self.assertFalse(self.config_path.exists())

    def test_skill_install_preview_refusal_and_explicit_replace(self) -> None:
        destination = (
            self.profile.resolve()
            / ".codex"
            / "skills"
            / "paper-curation"
        )
        first = self.run_setup("--install-skill", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        preview_value = cast(object, json.loads(first.stderr))
        self.assertIsInstance(preview_value, dict)
        preview = cast(dict[str, object], preview_value)
        self.assertEqual(
            Path(cast(str, preview["destination"])),
            destination,
        )
        first_value = cast(object, json.loads(first.stdout))
        self.assertIsInstance(first_value, dict)
        first_payload = cast(dict[str, object], first_value)
        self.assertEqual(
            Path(cast(str, first_payload["destination"])),
            destination,
        )
        self.assertEqual(
            (destination / "SKILL.md").read_bytes(),
            (ROOT / "SKILL.md").read_bytes(),
        )
        sentinel = destination / "operator-sentinel.txt"
        _ = sentinel.write_text("preserve", encoding="utf-8")

        refused = self.run_setup("--install-skill", "--json")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout)["status"], "exists")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

        replaced = self.run_setup(
            "--install-skill",
            "--replace-skill",
            "--json",
        )
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(
            sorted(
                path.relative_to(self.profile)
                for path in self.profile.rglob("*")
                if path.is_file()
            ),
            [Path(".codex/skills/paper-curation/SKILL.md")],
        )

    def test_profile_mismatch_fails_before_external_write(self) -> None:
        environment = self.profile_environment()
        environment["HOME"] = str(self.root / "foreign-home")

        result = self.run_setup(
            "--install-skill",
            "--json",
            environment=environment,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "profile-invalid")
        self.assertEqual(list(self.profile.iterdir()), [])

    def test_profile_resolver_does_not_touch_known_folder_when_env_agrees(
        self,
    ) -> None:
        calls = 0

        def forbidden_known_folder() -> Path:
            nonlocal calls
            calls += 1
            raise AssertionError("known folder must not be accessed")

        resolved = config_loader.resolve_user_profile(
            self.profile_environment(),
            known_folder=forbidden_known_folder,
        )

        self.assertEqual(resolved, self.profile.resolve())
        self.assertEqual(calls, 0)

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_profile_resolver_rejects_a_real_junction(self) -> None:
        target = self.root / "junction-target"
        target.mkdir()
        junction = self.root / "profile-junction"
        created = subprocess.run(
            [
                os.environ.get("ComSpec", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(junction),
                str(target),
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        environment = self.profile_environment()
        environment.update(
            {
                "HOME": str(junction),
                "HOMEDRIVE": junction.drive,
                "HOMEPATH": str(junction)[2:],
                "USERPROFILE": str(junction),
            }
        )

        with self.assertRaisesRegex(ValueError, "profile-invalid"):
            _ = config_loader.resolve_user_profile(environment)

        dangling = self.root / "dangling-profile-junction"
        missing_target = self.root / "outside" / "new-profile"
        created = subprocess.run(
            [
                os.environ.get("ComSpec", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(dangling),
                str(missing_target),
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        environment.update(
            {
                "HOME": str(dangling),
                "HOMEDRIVE": dangling.drive,
                "HOMEPATH": str(dangling)[2:],
                "USERPROFILE": str(dangling),
            }
        )
        with self.assertRaisesRegex(ValueError, "profile-invalid"):
            _ = config_loader.resolve_user_profile(environment)

        relative = environment.copy()
        relative.update(
            {
                "HOME": ".",
                "HOMEDRIVE": "",
                "HOMEPATH": "",
                "USERPROFILE": ".",
            }
        )
        with self.assertRaisesRegex(ValueError, "profile-invalid"):
            _ = config_loader.resolve_user_profile(relative)

    def test_zotero_ids_are_resolved_without_provider_fallback(self) -> None:
        requests: list[str] = []

        def opener(
            request: urllib.request.Request,
            **_kwargs: object,
        ) -> FakeResponse:
            url = request.full_url
            requests.append(url)
            if url.endswith("/keys/current"):
                return FakeResponse({"userID": 12345})
            return FakeResponse(
                [
                    {
                        "data": {
                            "key": "ABCD1234",
                            "name": "Fixture Collection",
                        }
                    }
                ]
            )

        resolved = setup.resolve_zotero_identifiers(
            "zotero-fixture-secret",
            "Fixture Collection",
            opener=opener,
        )

        self.assertEqual(
            resolved,
            setup.ZoteroIdentifiers("12345", "ABCD1234"),
        )
        self.assertEqual(len(requests), 2)
        self.assertTrue(all("zotero.org" in url for url in requests))

    def test_doctor_order_and_exit_semantics_are_stable(self) -> None:
        ready = doctor.CheckResult("pass", "ok")
        warn = doctor.CheckResult("warn", "cache-not-built")
        dependencies = doctor.DoctorDependencies.for_testing(
            python_runtime=lambda: ready,
            policy=lambda: ready,
            codex_attestation=lambda: ready,
            codex_login=lambda: ready,
            codex_canary=lambda: ready,
            disabled_features=lambda: ready,
            zotero=lambda: ready,
            bm25=lambda _topic: ready,
            geometry=lambda _topic: ready,
            loopback=lambda: ready,
            specter2=lambda: warn,
        )

        report = doctor.run_doctor(
            dependencies,
            mode="codex",
            topic=TOPIC,
            codex_canary=False,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.status, "ready")
        self.assertEqual(
            [row.id for row in report.checks],
            [
                "python-runtime",
                "runtime-policy",
                "codex-attestation",
                "codex-login",
                "codex-canary",
                "disabled-features",
                "zotero-local",
                "bm25",
                "geometry",
                "loopback",
                "specter2-cache",
            ],
        )
        self.assertEqual(report.checks[-1].status, "warn")

    def test_python_and_login_fail_before_model_or_zotero(self) -> None:
        calls: list[str] = []

        def result(
            name: str,
            status: doctor.CheckStatus = "pass",
        ) -> doctor.CheckResult:
            calls.append(name)
            return doctor.CheckResult(status, name)

        dependencies = doctor.DoctorDependencies.for_testing(
            python_runtime=lambda: result("python", "fail"),
            policy=lambda: result("policy"),
            codex_attestation=lambda: result("attestation"),
            codex_login=lambda: result("login"),
            codex_canary=lambda: result("model"),
            disabled_features=lambda: result("disabled"),
            zotero=lambda: result("zotero"),
            bm25=lambda _topic: result("bm25"),
            geometry=lambda _topic: result("geometry"),
            loopback=lambda: result("loopback"),
            specter2=lambda: result("specter2"),
        )
        python_failure = doctor.run_doctor(
            dependencies,
            mode="codex",
            topic=TOPIC,
            codex_canary=True,
        )
        self.assertEqual(python_failure.exit_code, 2)
        self.assertEqual(calls, ["python"])

        calls.clear()
        dependencies = doctor.DoctorDependencies.for_testing(
            python_runtime=lambda: result("python"),
            policy=lambda: result("policy"),
            codex_attestation=lambda: result("attestation"),
            codex_login=lambda: result("login", "fail"),
            codex_canary=lambda: result("model"),
            disabled_features=lambda: result("disabled"),
            zotero=lambda: result("zotero"),
            bm25=lambda _topic: result("bm25"),
            geometry=lambda _topic: result("geometry"),
            loopback=lambda: result("loopback"),
            specter2=lambda: result("specter2"),
        )
        login_failure = doctor.run_doctor(
            dependencies,
            mode="codex",
            topic=TOPIC,
            codex_canary=True,
        )
        self.assertEqual(login_failure.exit_code, 1)
        self.assertEqual(
            calls,
            ["python", "policy", "attestation", "login"],
        )

    def test_canary_is_explicit_and_off_mode_skips_codex(self) -> None:
        calls: list[str] = []
        ready = doctor.CheckResult("pass", "ok")

        def called(name: str) -> doctor.CheckResult:
            calls.append(name)
            return ready

        dependencies = doctor.DoctorDependencies.for_testing(
            python_runtime=lambda: called("python"),
            policy=lambda: called("policy"),
            codex_attestation=lambda: called("attestation"),
            codex_login=lambda: called("login"),
            codex_canary=lambda: called("canary"),
            disabled_features=lambda: called("disabled"),
            zotero=lambda: called("zotero"),
            bm25=lambda _topic: called("bm25"),
            geometry=lambda _topic: called("geometry"),
            loopback=lambda: called("loopback"),
            specter2=lambda: doctor.CheckResult("warn", "missing"),
        )

        normal = doctor.run_doctor(
            dependencies,
            mode="codex",
            topic=None,
            codex_canary=False,
        )
        self.assertEqual(normal.exit_code, 0)
        self.assertNotIn("canary", calls)

        calls.clear()
        off = doctor.run_doctor(
            dependencies,
            mode="off",
            topic=None,
            codex_canary=False,
        )
        self.assertEqual(off.exit_code, 0)
        self.assertNotIn("attestation", calls)
        self.assertNotIn("login", calls)
        self.assertNotIn("canary", calls)

    def test_local_zotero_status_is_value_free_and_network_free(self) -> None:
        config = local_config(self.pdf_dir)
        status = config_loader.local_zotero_status(
            config
        )

        self.assertEqual(
            status,
            {
                "api_key_configured": True,
                "collection_count": 1,
                "email_configured": True,
                "pdf_dir_configured": True,
                "pdf_dir_exists": True,
                "user_id_configured": True,
            },
        )
        serialized = json.dumps(status, sort_keys=True)
        self.assertNotIn("zotero-fixture-secret", serialized)
        self.assertNotIn(str(self.pdf_dir), serialized)

    def test_non_string_zotero_values_are_not_ready(self) -> None:
        invalid = local_config(self.pdf_dir)
        zotero = cast(dict[str, object], invalid["zotero"])
        zotero.update(
            {
                "api_key": None,
                "collections": {TOPIC: None},
                "email": None,
                "user_id": None,
            }
        )

        status = config_loader.local_zotero_status(invalid)

        self.assertFalse(status["api_key_configured"])
        self.assertFalse(status["email_configured"])
        self.assertFalse(status["user_id_configured"])
        self.assertEqual(status["collection_count"], 0)
        _ = self.config_path.write_text(
            json.dumps(invalid),
            encoding="utf-8",
        )
        result = self.run_setup(
            "--config",
            str(self.config_path),
            "--json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "config-incomplete")

    def test_doctor_reads_no_saved_auth_file(self) -> None:
        marker = self.profile / ".codex" / "auth.json"
        marker.parent.mkdir(parents=True)
        _ = marker.write_text("must-not-be-read", encoding="utf-8")
        _ = self.config_path.write_text(
            json.dumps(local_config(self.pdf_dir)),
            encoding="utf-8",
        )
        production = doctor.ProductionDoctor(
            ROOT,
            self.config_path,
            self.root / "docs",
            "codex",
            self.profile,
        )
        original_read_text = Path.read_text
        reads: list[Path] = []

        def guarded_read_text(
            path: Path,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> str:
            if path == marker:
                reads.append(path)
                raise AssertionError("credential file was read")
            _ = newline
            return original_read_text(
                path,
                encoding=encoding,
                errors=errors,
            )

        fake_gateway = cast(
            CodexGateway,
            cast(object, FakePreflightGateway()),
        )
        with (
            patch.object(production, "gateway", return_value=fake_gateway),
            patch.object(Path, "read_text", guarded_read_text),
        ):
            status = production.codex_login()

        self.assertEqual(status, doctor.CheckResult("pass", "codex-login-valid"))
        self.assertEqual(reads, [])

    def test_doctor_accepts_the_produced_geometry_manifest(self) -> None:
        docs = self.root / "docs"
        paper = docs / "papers" / "001_Alpha"
        figures = paper / "figures"
        figures.mkdir(parents=True)
        image = figures / "fig1.png"
        _ = image.write_bytes(b"geometry-fixture")
        image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        _ = (figures / "manifest-v1.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "caption": "Figure 1.",
                            "page": 0,
                            "path": "figures/fig1.png",
                            "sha256": image_hash,
                        }
                    ],
                    "schema": "geometry-figures-v1",
                    "source_pdf_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        topic_dir = docs / TOPIC
        topic_dir.mkdir()
        _ = (topic_dir / "_search_index.json").write_text(
            json.dumps({"documents": [{"slug": "001_Alpha"}]}),
            encoding="utf-8",
        )
        _ = self.config_path.write_text(
            json.dumps(local_config(self.pdf_dir)),
            encoding="utf-8",
        )
        production = doctor.ProductionDoctor(
            ROOT,
            self.config_path,
            docs,
            "off",
            self.profile,
        )

        status = production.geometry(TOPIC)

        self.assertEqual(status, doctor.CheckResult("pass", "geometry-ready"))

    def test_gateway_preflight_checks_login_without_model_execution(
        self,
    ) -> None:
        executable = self.root / "codex-fixture.exe"
        _ = executable.write_bytes(b"signed-fixture")
        runner = FakeRunner()
        with patch.dict(
            os.environ,
            {"PAPER_CURATION_TESTING": "1"},
        ):
            gateway = CodexGateway.for_testing(
                GatewayPaths(
                    ROOT,
                    executable,
                    self.root / "codex-resolved.json",
                    True,
                ),
                runner,
            )
            _ = gateway.requalify(accept=True)
            runner.calls.clear()
            inventory = gateway.preflight()

        self.assertTrue(inventory["attested"])
        self.assertTrue(
            any(call.argv[1:] == ("login", "status") for call in runner.calls)
        )
        self.assertFalse(
            any(call.argv[1:2] == ("exec",) for call in runner.calls)
        )

    def test_doctor_process_has_stable_zero_one_two_exits(
        self,
    ) -> None:
        _ = self.config_path.write_text(
            json.dumps(local_config(self.pdf_dir)),
            encoding="utf-8",
        )
        profile_before = list(self.profile.iterdir())
        ready = self.run_doctor_cli(
            "--config",
            str(self.config_path),
            "--docs-dir",
            str(self.root / "docs"),
            "--llm-mode",
            "off",
            "--format",
            "json",
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(json.loads(ready.stdout)["status"], "ready")
        self.assertEqual(list(self.profile.iterdir()), profile_before)

        incomplete = local_config(self.pdf_dir)
        cast(dict[str, object], incomplete["zotero"])["collections"] = {}
        _ = self.config_path.write_text(
            json.dumps(incomplete),
            encoding="utf-8",
        )
        not_ready = self.run_doctor_cli(
            "--config",
            str(self.config_path),
            "--llm-mode",
            "off",
            "--format",
            "json",
        )
        self.assertEqual(not_ready.returncode, 1, not_ready.stderr)
        self.assertEqual(
            json.loads(not_ready.stdout)["status"],
            "not-ready",
        )

        mismatch = self.profile_environment()
        mismatch["HOME"] = str(self.root / "foreign-home")
        invalid = self.run_doctor_cli(
            "--config",
            str(self.config_path),
            "--format",
            "json",
            environment=mismatch,
        )
        self.assertEqual(invalid.returncode, 2, invalid.stderr)
        self.assertEqual(json.loads(invalid.stdout)["status"], "error")


if __name__ == "__main__":
    _ = unittest.main()
