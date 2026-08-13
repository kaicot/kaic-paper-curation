"""Process probe for the versioned loopback local-answer HTTP contract."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.lib.local_answer_contract import (  # noqa: E402
    CONTRACT_VERSION,
    MAX_BODY_BYTES,
    MAX_QUERY_CHARS,
    RESPONSE_SCHEMA,
)
from pipeline.lib.run_state import TopicLock  # noqa: E402

JsonObject = dict[str, object]


class ProbeError(RuntimeError):
    pass


def _request(
    base_url: str,
    path: str,
    *,
    body: bytes | None = None,
    token: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, JsonObject, dict[str, str]]:
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Origin"] = base_url
        headers["X-CSRF-Token"] = token
    if extra_headers is not None:
        headers.update(extra_headers)
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        response = cast(
            urllib.response.addinfourl,
            urllib.request.urlopen(request, timeout=30),
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            value = cast(object, json.loads(raw))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProbeError(f"{path}: response is not JSON") from error
        if not isinstance(value, dict):
            raise ProbeError(f"{path}: response root is not an object")
        return (
            cast(int, response.status),
            cast(JsonObject, value),
            dict(response.headers),
        )


def _body(
    value: Mapping[str, object],
    *,
    exact_size: int | None = None,
) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if exact_size is None:
        return raw
    if len(raw) > exact_size:
        raise ProbeError("fixture exceeds requested exact body size")
    return raw + (b" " * (exact_size - len(raw)))


def _assert_status(
    actual: int,
    expected: int,
    label: str,
) -> None:
    if actual != expected:
        raise ProbeError(
            f"{label}: expected HTTP {expected}, received {actual}"
        )


def probe(
    base_url: str,
    *,
    topic: str,
    query: str,
    length: str,
    include_boundaries: bool,
    docs_dir: Path,
) -> JsonObject:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path not in {"", "/"}
    ):
        raise ProbeError("base URL must be http://127.0.0.1:<port>")
    base_url = base_url.rstrip("/")
    health_status, health, health_headers = _request(
        base_url,
        "/api/health",
    )
    _assert_status(health_status, 200, "health")
    if (
        health.get("ok") is not True
        or health.get("service") != "paper-curation-serve-local"
    ):
        raise ProbeError("health contract mismatch")
    session_status, session, _ = _request(base_url, "/api/session")
    _assert_status(session_status, 200, "session")
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        raise ProbeError("session token missing")
    request = {
        "length": length,
        "query": query,
        "topic": topic,
    }
    answer_status, answer, answer_headers = _request(
        base_url,
        "/api/answer",
        body=_body(request),
        token=token,
    )
    _assert_status(answer_status, 200, "answer")
    if answer.get("schema") != RESPONSE_SCHEMA:
        raise ProbeError("answer schema mismatch")
    if (
        "Access-Control-Allow-Origin" in answer_headers
        or "Access-Control-Allow-Origin" in health_headers
    ):
        raise ProbeError("CORS header present")

    checks: list[JsonObject] = [
        {"case": "health", "status": health_status},
        {"case": "answer", "status": answer_status},
    ]
    if include_boundaries:
        boundary_cases = [
            (
                "body-max",
                _body(request, exact_size=MAX_BODY_BYTES),
                200,
            ),
            (
                "body-over",
                b" " * (MAX_BODY_BYTES + 1),
                413,
            ),
            (
                "query-over",
                _body(
                    {
                        "length": "short",
                        "query": "a" * (MAX_QUERY_CHARS + 1),
                        "topic": topic,
                    }
                ),
                413,
            ),
            (
                "topic-invalid",
                _body(
                    {
                        "length": "short",
                        "query": query,
                        "topic": "../escape",
                    }
                ),
                422,
            ),
            (
                "length-invalid",
                _body(
                    {
                        "length": "invalid",
                        "query": query,
                        "topic": topic,
                    }
                ),
                422,
            ),
            (
                "topic-missing",
                _body(
                    {
                        "length": "short",
                        "query": query,
                    }
                ),
                404,
            ),
        ]
        for label, body, expected in boundary_cases:
            status, _, headers = _request(
                base_url,
                "/api/answer",
                body=body,
                token=token,
            )
            _assert_status(status, expected, label)
            if "Access-Control-Allow-Origin" in headers:
                raise ProbeError(f"{label}: CORS header present")
            checks.append({"case": label, "status": status})
        lock = TopicLock.acquire(
            docs_dir
            / ".local-answer-locks"
            / f"{topic}.lock",
            topic,
        )
        try:
            contention_status, _, _ = _request(
                base_url,
                "/api/answer",
                body=_body(request),
                token=token,
            )
        finally:
            lock.release()
        _assert_status(contention_status, 409, "contention")
        checks.append(
            {
                "case": "contention",
                "status": 409,
            }
        )
        mismatch_status, _, _ = _request(
            base_url,
            "/api/answer",
            body=_body(request),
            token=token,
            extra_headers={
                "X-Local-Answer-Probe": "citation-mismatch-v1"
            },
        )
        _assert_status(
            mismatch_status,
            502,
            "citation-mismatch",
        )
        checks.append(
            {
                "case": "citation-mismatch",
                "status": 502,
            }
        )
        for route in ("/api/embed", "/api/audio-email", "/api/citedby"):
            status, _, _ = _request(
                base_url,
                route,
                body=b"{}",
                token=token,
            )
            _assert_status(status, 404, route)
            checks.append({"case": route, "status": status})
    return {
        "base_url": base_url,
        "checks": checks,
        "contract": CONTRACT_VERSION,
        "result": "PASS",
        "schema": "local-answer-probe-v1",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--base-url", required=True)
    _ = parser.add_argument(
        "--contract",
        choices=(CONTRACT_VERSION,),
        required=True,
    )
    _ = parser.add_argument("--include-boundaries", action="store_true")
    _ = parser.add_argument(
        "--docs-dir",
        type=Path,
        default=ROOT / "docs",
    )
    _ = parser.add_argument("--length", choices=("short", "medium", "long"), default="short")
    _ = parser.add_argument("--query", default="alpha")
    _ = parser.add_argument("--topic", default="qa_fixture")
    arguments = parser.parse_args(argv)
    try:
        report = probe(
            cast(str, arguments.base_url),
            topic=cast(str, arguments.topic),
            query=cast(str, arguments.query),
            length=cast(str, arguments.length),
            include_boundaries=cast(bool, arguments.include_boundaries),
            docs_dir=cast(Path, arguments.docs_dir),
        )
    except (OSError, ProbeError) as error:
        print(f"local-answer probe failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
