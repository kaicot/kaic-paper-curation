"""Static docs and bounded saved-auth answers on a loopback-only listener."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, cast, override
from urllib.parse import urlsplit

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config_loader import load_config  # noqa: E402
from pipeline.lib.local_answer_contract import (  # noqa: E402
    ERROR_SCHEMA,
    MAX_BODY_BYTES,
    READY_SCHEMA,
)
from pipeline.local_answer_service import (  # noqa: E402
    AnswerGenerator,
    LocalAnswerCodex,
    LocalAnswerError,
    LocalAnswerService,
)
from pipeline.runtime_policy import (  # noqa: E402
    RuntimePolicyError,
    resolve_runtime_policy,
)
from pipeline.schemas.codex_schema import JsonObject  # noqa: E402

DOCS_DIR = PROJECT_ROOT / "docs"
_FORBIDDEN_STATIC_NAMES = frozenset(
    {
        "_local_keys.json",
        "_zotero_keys.json",
    }
)


def _write_ready_file(
    path: Path,
    value: Mapping[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        _ = handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class _UnavailableGenerator:
    def generate(self, **_: object) -> dict[str, object]:
        raise LocalAnswerError(503, "runtime-off")


class LocalAnswerHTTPServer(ThreadingHTTPServer):
    answer_service: LocalAnswerService = cast(
        LocalAnswerService,
        cast(object, None),
    )
    csrf_token: str = ""
    docs_dir: Path = Path()
    public_url: str = ""


class LocalHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def local_server(self) -> LocalAnswerHTTPServer:
        return cast(LocalAnswerHTTPServer, self.server)

    @override
    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "paper-curation-serve-local",
                },
            )
            return
        if route == "/api/session":
            self._send_json(
                200,
                {
                    "csrf_token": self.local_server.csrf_token,
                    "schema": "local-answer-session-v1",
                },
            )
            return
        if route.startswith("/api/"):
            self._send_error(404, "not-found")
            return
        super().do_GET()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_error(404, "not-found")

    @override
    def handle_expect_100(self) -> bool:
        try:
            self._preflight()
        except LocalAnswerError as error:
            self._send_error(error.status, error.code)
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route != "/api/answer":
            self._send_error(404, "not-found")
            return
        preflight = self._preflight()
        if preflight is not None:
            self._send_error(*preflight)
            return
        try:
            request = self._read_request()
            probe = self.headers.get("X-Local-Answer-Probe")
            if probe is None:
                response = self.local_server.answer_service.answer(
                    request
                )
            elif probe == "citation-mismatch-v1":
                response = (
                    self.local_server.answer_service
                    .probe_citation_mismatch(request)
                )
            else:
                raise LocalAnswerError(400, "probe-invalid")
        except LocalAnswerError as error:
            self._send_error(error.status, error.code)
            return
        self._send_json(200, response)

    def _preflight(self) -> tuple[int, str] | None:
        expected = urlsplit(self.local_server.public_url)
        if self.headers.get("Host") != expected.netloc:
            return 400, "host-invalid"
        origin = self.headers.get("Origin")
        if origin is not None:
            if origin != self.local_server.public_url:
                return 403, "origin-invalid"
            if (
                self.headers.get("X-CSRF-Token")
                != self.local_server.csrf_token
            ):
                return 403, "csrf-invalid"
        if self.headers.get("Transfer-Encoding") is not None:
            return 400, "transfer-encoding-denied"
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            return 400, "content-length-invalid"
        try:
            length = int(lengths[0])
        except ValueError:
            return 400, "content-length-invalid"
        if length < 0:
            return 400, "content-length-invalid"
        if length > MAX_BODY_BYTES:
            return 413, "body-too-large"
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return 415, "content-type-invalid"
        return None

    def _read_request(self) -> dict[str, object]:
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise LocalAnswerError(400, "body-incomplete")

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        try:
            value = cast(
                object,
                json.loads(
                    raw,
                    object_pairs_hook=pairs,
                    parse_constant=lambda token: (
                        (_ for _ in ()).throw(
                            ValueError(f"non-finite constant: {token}")
                        )
                    ),
                ),
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise LocalAnswerError(400, "json-invalid") from error
        if not isinstance(value, dict):
            raise LocalAnswerError(400, "request-schema-invalid")
        return cast(dict[str, object], value)

    def _send_error(self, code: int, status: str) -> None:
        self._send_json(
            code,
            {
                "schema": ERROR_SCHEMA,
                "status": status,
            },
        )

    def _send_json(self, code: int, value: object) -> None:
        body = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            _ = self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    @override
    def send_head(self) -> BinaryIO | None:
        route = self.path.split("?", 1)[0]
        parts = [part for part in Path(route).parts if part not in {"/", "\\"}]
        if any(
            part.startswith(".")
            or part.casefold() in _FORBIDDEN_STATIC_NAMES
            for part in parts
        ):
            self.send_error(404)
            return None
        translated = Path(self.translate_path(self.path))
        try:
            resolved = translated.resolve()
        except OSError:
            self.send_error(404)
            return None
        docs = self.local_server.docs_dir
        if (
            resolved != docs
            and docs not in resolved.parents
        ):
            self.send_error(404)
            return None
        relative_parts = translated.relative_to(docs).parts
        if any(
            part.startswith(".")
            or part.casefold() in _FORBIDDEN_STATIC_NAMES
            for part in relative_parts
        ):
            self.send_error(404)
            return None
        cursor = translated
        while cursor != docs:
            if cursor.is_symlink():
                self.send_error(404)
                return None
            cursor = cursor.parent
        return super().send_head()


def create_server(
    host: str,
    port: int,
    docs_dir: Path,
    answer_service: LocalAnswerService,
) -> LocalAnswerHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("host must be 127.0.0.1")
    if isinstance(port, bool) or not 0 <= port <= 65_535:
        raise ValueError("port must be 0..65535")
    if docs_dir.is_symlink():
        raise ValueError("docs directory must not be a symlink")
    root = docs_dir.resolve()
    if not root.is_dir():
        raise ValueError("docs directory must be a regular directory")
    handler = partial(LocalHandler, directory=str(root))
    server = LocalAnswerHTTPServer((host, port), handler)
    bound_host, bound_port = server.server_address[:2]
    server.answer_service = answer_service
    server.csrf_token = secrets.token_urlsafe(32)
    server.docs_dir = root
    server.public_url = f"http://{bound_host}:{bound_port}"
    server.daemon_threads = True
    return server


@dataclass(frozen=True, slots=True)
class Arguments:
    docs_dir: Path
    host: str
    llm_mode: str | None
    port: int
    ready_file: Path | None


def _arguments(argv: list[str] | None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--docs-dir", type=Path, default=DOCS_DIR)
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--llm-mode", choices=("codex", "off"))
    _ = parser.add_argument("--port", type=int, default=0)
    _ = parser.add_argument("--ready-file", type=Path)
    namespace = parser.parse_args(argv)
    return Arguments(
        docs_dir=cast(Path, namespace.docs_dir),
        host=cast(str, namespace.host),
        llm_mode=cast(str | None, namespace.llm_mode),
        port=cast(int, namespace.port),
        ready_file=cast(Path | None, namespace.ready_file),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    if arguments.host != "127.0.0.1":
        print("serve-local denied: host must be 127.0.0.1", file=sys.stderr)
        return 2
    try:
        policy = resolve_runtime_policy(
            cast(JsonObject, cast(object, load_config())),
            arguments.llm_mode,
        )
        generator: AnswerGenerator
        if policy.mode == "codex":
            generator = LocalAnswerCodex.production(
                PROJECT_ROOT,
                policy,
                arguments.docs_dir,
            )
        else:
            generator = _UnavailableGenerator()
        service = LocalAnswerService(arguments.docs_dir, generator)
        server = create_server(
            arguments.host,
            arguments.port,
            arguments.docs_dir,
            service,
        )
    except (LocalAnswerError, RuntimePolicyError, OSError, ValueError) as error:
        print(f"serve-local denied: {error}", file=sys.stderr)
        return 2

    ready_file = arguments.ready_file
    try:
        if ready_file is not None:
            _write_ready_file(
                ready_file,
                {
                    "host": server.server_address[0],
                    "pid": os.getpid(),
                    "port": server.server_address[1],
                    "schema": READY_SCHEMA,
                    "schema_version": 1,
                    "url": server.public_url,
                },
            )
        print(
            json.dumps(
                {
                    "event": "listening",
                    "host": server.server_address[0],
                    "port": server.server_address[1],
                    "url": server.public_url,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if ready_file is not None:
            try:
                _ = ready_file.unlink(missing_ok=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
