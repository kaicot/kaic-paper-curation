"""Loopback-only static server with a deterministic cited-by route."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast, override


PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))


class CitedbyModule(Protocol):
    def run_citedby(self, doi: str, **kwargs: object) -> object: ...


citedby_core = cast(
    CitedbyModule,
    cast(object, importlib.import_module("lib.citedby")),
)


DOCS_DIR = PROJECT_ROOT / "docs"


def _safe_slug(value: object) -> str:
    slug = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", str(value or ""))
    slug = slug.replace("..", "_").strip("._")
    return slug[:120]


class LocalHandler(SimpleHTTPRequestHandler):
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/api/citedby":
            self._handle_citedby()
            return
        self._send_json(404, {"error": "not found"})

    def _send_json(self, code: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)
        self.wfile.flush()

    def _read_json(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            value = cast(object, json.loads(raw))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return cast(dict[str, object], value) if isinstance(value, dict) else None

    def _stream_line(self, value: object) -> bool:
        try:
            _ = self.wfile.write(
                (
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
            )
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _handle_citedby(self) -> None:
        request = self._read_json()
        if request is None:
            self._send_json(400, {"error": "invalid json"})
            return
        doi = str(request.get("doi", "")).strip()
        if not doi:
            self._send_json(400, {"error": "doi required"})
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/x-ndjson; charset=utf-8",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def on_event(
            phase: str,
            message: str,
            current: int = 0,
            total: int = 0,
        ) -> None:
            _ = self._stream_line(
                {
                    "current": current,
                    "event": "progress",
                    "message": message,
                    "phase": phase,
                    "total": total,
                }
            )

        try:
            result = citedby_core.run_citedby(
                doi,
                topic=request.get("topic"),
                lang=str(request.get("lang", "ko")),
                sources=request.get("sources"),
                use_llm_originality=bool(
                    request.get("use_llm_originality", False)
                ),
                on_event=on_event,
            )
            value = cast(dict[str, object], result)
            files = self._save_outputs(value, request.get("slug"))
            _ = self._stream_line(
                {
                    "event": "done",
                    "files": files,
                    "matched": value.get("matched", 0),
                    "report_html": value.get("report_html", ""),
                    "source_counts": value.get("source_counts", {}),
                    "total": value.get("total", 0),
                }
            )
        except Exception as error:
            _ = self._stream_line(
                {
                    "event": "error",
                    "message": str(error),
                }
            )

    def _save_outputs(
        self,
        result: dict[str, object],
        raw_slug: object,
    ) -> dict[str, str]:
        slug = _safe_slug(raw_slug)
        if not slug:
            return {}
        directory = Path(DOCS_DIR) / "papers" / slug / "citedby"
        directory.mkdir(parents=True, exist_ok=True)
        doi = _safe_slug(result.get("doi", "result")) or "result"
        report = directory / f"{doi}.html"
        table = directory / f"{doi}.csv"
        _ = report.write_text(
            str(result.get("report_html", "")),
            encoding="utf-8",
        )
        _ = table.write_text(
            str(result.get("csv", "")),
            encoding="utf-8",
        )
        base = f"/papers/{slug}/citedby"
        return {
            "csv": f"{base}/{table.name}",
            "report": f"{base}/{report.name}",
        }


@dataclass(frozen=True, slots=True)
class Arguments:
    host: str
    port: int


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", type=int, default=8000)
    namespace = parser.parse_args(argv)
    arguments = Arguments(
        host=cast(str, namespace.host),
        port=cast(int, namespace.port),
    )
    handler = partial(LocalHandler, directory=str(DOCS_DIR))
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler)
    print(f"http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
