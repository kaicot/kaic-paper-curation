"""Local-only setup for saved-auth Codex paper curation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast, final


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = PROJECT_ROOT / "pipeline"
for candidate in (PROJECT_ROOT, PIPELINE_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pipeline.config_loader import (  # noqa: E402
    local_zotero_status,
    resolve_user_profile,
)
from pipeline.runtime_policy import (  # noqa: E402
    JsonObject,
    RuntimePolicyError,
    resolve_runtime_policy,
)


@final
class SetupError(RuntimeError):
    status: str
    exit_code: int

    def __init__(self, status: str, exit_code: int = 1) -> None:
        super().__init__(status)
        self.status = status
        self.exit_code = exit_code


class ReadableResponse(Protocol):
    def __enter__(self) -> "ReadableResponse": ...

    def __exit__(self, *args: object) -> object: ...

    def read(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ZoteroIdentifiers:
    user_id: str
    collection_key: str


@dataclass(frozen=True, slots=True)
class Arguments:
    config: Path
    install_skill: bool
    json_output: bool
    replace_skill: bool


def _load_object(path: Path) -> JsonObject:
    def reject_duplicate(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SetupError("config-invalid", 2)
            result[key] = value
        return result

    try:
        value = cast(
            object,
            json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicate,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    SetupError("config-invalid", 2)
                ),
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SetupError("config-invalid", 2) from error
    if not isinstance(value, dict):
        raise SetupError("config-invalid", 2)
    return cast(JsonObject, value)


def _atomic_write(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_zotero_identifiers(
    api_key: str,
    collection_name: str,
    *,
    opener: Callable[..., ReadableResponse] | None = None,
) -> ZoteroIdentifiers:
    if not api_key.strip() or not collection_name.strip():
        raise SetupError("zotero-input-invalid")
    first = urllib.request.Request(
        "https://api.zotero.org/keys/current",
        headers={
            "User-Agent": "paper-curation-local-setup",
            "Zotero-API-Key": api_key,
        },
    )
    open_request = opener or cast(
        Callable[..., ReadableResponse],
        urllib.request.urlopen,
    )
    try:
        with open_request(first, timeout=15) as response:
            identity_value = cast(object, json.loads(response.read()))
        if not isinstance(identity_value, dict):
            raise SetupError("zotero-resolution-failed")
        identity = cast(dict[str, object], identity_value)
        user_id = str(identity.get("userID", "")).strip()
        if not user_id:
            raise SetupError("zotero-resolution-failed")
        second = urllib.request.Request(
            (
                f"https://api.zotero.org/users/{user_id}/collections"
                "?format=json&limit=100"
            ),
            headers={
                "User-Agent": "paper-curation-local-setup",
                "Zotero-API-Key": api_key,
            },
        )
        with open_request(second, timeout=15) as response:
            collections_value = cast(
                object,
                json.loads(response.read()),
            )
    except SetupError:
        raise
    except Exception as error:
        raise SetupError("zotero-resolution-failed") from error
    if not isinstance(collections_value, list):
        raise SetupError("zotero-resolution-failed")
    raw_collections = cast(list[object], collections_value)
    collection_key = ""
    for item in raw_collections:
        if not isinstance(item, dict):
            continue
        data = cast(dict[str, object], item).get("data")
        if (
            isinstance(data, dict)
            and cast(dict[str, object], data).get("name")
            == collection_name
        ):
            collection_key = str(
                cast(dict[str, object], data).get("key", "")
            ).strip()
            break
    if not collection_key:
        raise SetupError("zotero-collection-not-found")
    return ZoteroIdentifiers(user_id, collection_key)


def _skill_destination(profile: Path) -> Path:
    destination = profile / ".codex" / "skills" / "kaic-paper-curation"
    resolved_parent = destination.parent.resolve()
    try:
        _ = resolved_parent.relative_to(profile.resolve())
    except ValueError as error:
        raise SetupError("profile-invalid", 2) from error
    return destination


def install_skill(
    profile: Path,
    *,
    replace: bool,
    source: Path | None = None,
) -> Path:
    shipped = PROJECT_ROOT / "SKILL.md" if source is None else source
    if (
        not shipped.is_file()
        or shipped.is_symlink()
        or profile.is_symlink()
    ):
        raise SetupError("skill-source-invalid", 2)
    destination = _skill_destination(profile)
    if destination.exists() or destination.is_symlink():
        if not replace:
            raise SetupError("exists")
        if destination.is_symlink() or not destination.is_dir():
            raise SetupError("destination-invalid", 2)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=".kaic-paper-curation-stage-",
        )
    )
    backup: Path | None = None
    try:
        staged_skill = stage / "SKILL.md"
        _ = staged_skill.write_bytes(shipped.read_bytes())
        if destination.exists():
            backup = parent / (
                ".kaic-paper-curation-backup-" + uuid.uuid4().hex
            )
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except Exception:
            if backup is not None and backup.exists():
                os.replace(backup, destination)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return destination
    except SetupError:
        raise
    except Exception as error:
        raise SetupError("install-failed", 2) from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _validated_existing_config(path: Path) -> JsonObject:
    config = _load_object(path)
    try:
        policy = resolve_runtime_policy(
            config
        )
    except RuntimePolicyError as error:
        raise SetupError("config-invalid", 2) from error
    if policy.mode != "codex":
        raise SetupError("config-not-codex")
    status = local_zotero_status(config)
    required = (
        "api_key_configured",
        "email_configured",
        "pdf_dir_configured",
        "pdf_dir_exists",
        "user_id_configured",
    )
    if (
        not all(status[name] is True for name in required)
        or int(status["collection_count"]) < 1
    ):
        raise SetupError("config-incomplete")
    return config


def _prompt_local_config() -> JsonObject:
    if not _interactive_stdin():
        raise SetupError("input-required")
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("Zotero API key: ").strip()
    email = input("Zotero/Unpaywall email: ").strip()
    collection = input("Zotero collection name: ").strip()
    topic = input("Topic alias: ").strip()
    pdf_dir = Path(
        input("Zotero PDF directory: ").strip()
    ).expanduser()
    if (
        not api_key
        or not email
        or not collection
        or not topic
        or not pdf_dir.is_dir()
        or pdf_dir.is_symlink()
    ):
        raise SetupError("input-invalid")
    resolved = resolve_zotero_identifiers(api_key, collection)
    return {
        "runtime": {
            "allow_paid_api": False,
            "llm_mode": "codex",
            "schema_version": 2,
        },
        "schema_version": 2,
        "unpaywall_email": email,
        "zotero": {
            "api_key": api_key,
            "collections": {topic: resolved.collection_key},
            "email": email,
            "pdf_dir": str(pdf_dir.resolve()),
            "user_id": resolved.user_id,
        },
    }


def _interactive_stdin() -> bool:
    if os.name != "nt":
        return sys.stdin.isatty()
    import ctypes
    import msvcrt

    mode = ctypes.c_ulong()
    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return False
    result = cast(
        int,
        ctypes.windll.kernel32.GetConsoleMode(
            handle,
            ctypes.byref(mode),
        ),
    )
    return bool(result)


def _arguments(argv: list[str] | None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.json",
    )
    _ = parser.add_argument("--install-skill", action="store_true")
    _ = parser.add_argument("--replace-skill", action="store_true")
    _ = parser.add_argument("--json", action="store_true")
    namespace = parser.parse_args(argv)
    install = cast(bool, namespace.install_skill)
    replace = cast(bool, namespace.replace_skill)
    if replace and not install:
        parser.error("--replace-skill requires --install-skill")
    return Arguments(
        config=cast(Path, namespace.config),
        install_skill=install,
        json_output=cast(bool, namespace.json),
        replace_skill=replace,
    )


def _emit(
    value: Mapping[str, object],
    *,
    json_output: bool,
    stream: IO[str] | None = None,
) -> None:
    target = sys.stdout if stream is None else stream
    if json_output:
        print(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=target,
        )
        return
    print(
        str(value.get("message", value.get("status", ""))),
        file=target,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _arguments(argv)
        if arguments.install_skill:
            try:
                profile = resolve_user_profile()
            except ValueError as error:
                raise SetupError("profile-invalid", 2) from error
            destination = _skill_destination(profile)
            print_value: dict[str, object] = {
                "destination": str(destination),
                "message": f"Skill destination: {destination}",
                "schema": "local-setup-v1",
                "schema_version": 1,
                "status": "preview",
            }
            _emit(
                print_value,
                json_output=arguments.json_output,
                stream=sys.stderr,
            )
            try:
                installed = install_skill(
                    profile,
                    replace=arguments.replace_skill,
                )
            except SetupError as error:
                print_value["status"] = error.status
                _emit(print_value, json_output=arguments.json_output)
                return error.exit_code
            print_value["destination"] = str(installed)
            print_value["status"] = "installed"
            _emit(print_value, json_output=arguments.json_output)
            return 0
        if arguments.config.exists():
            _ = _validated_existing_config(arguments.config)
        else:
            config = _prompt_local_config()
            _atomic_write(arguments.config, config)
        _emit(
            {
                "message": "Local setup is ready.",
                "schema": "local-setup-v1",
                "schema_version": 1,
                "status": "ready",
            },
            json_output=arguments.json_output,
        )
        return 0
    except SetupError as error:
        _emit(
            {
                "schema": "local-setup-v1",
                "schema_version": 1,
                "status": error.status,
            },
            json_output="--json" in (argv or sys.argv[1:]),
        )
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
