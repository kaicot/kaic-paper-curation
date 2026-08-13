"""Contract and process coverage for the loopback local-answer route."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlsplit

from pipeline.lib.local_answer_contract import (
    MAX_BODY_BYTES,
    MAX_CONTEXT_CHARS,
    MAX_QUERY_CHARS,
    MAX_RESPONSE_BYTES,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
)
from pipeline.local_answer_service import (
    EvidenceChunk,
    LocalAnswerCodex,
    LocalAnswerError,
    LocalAnswerService,
)
from pipeline.providers.codex_gateway import CodexGateway, GatewayPaths
from pipeline.lib.run_state import TopicLock
from pipeline.runtime_policy import RuntimePolicy
from pipeline.serve_local import create_server
from pipeline.sparse_index import build_sparse_index
from pipeline.tests.test_codex_exec import (
    FakeRunner,
    JsonObject as RunnerJsonObject,
)


class FakeAnswerGenerator:
    calls: list[tuple[str, str, str, list[EvidenceChunk]]]
    response: dict[str, object] | None

    def __init__(self) -> None:
        self.calls = []
        self.response = None

    def generate(
        self,
        *,
        topic: str,
        query: str,
        length: str,
        chunks: list[EvidenceChunk],
    ) -> dict[str, object]:
        self.calls.append((topic, query, length, chunks))
        if self.response is not None:
            return self.response
        first = chunks[0]
        return {
            "answer": "근거 기반 답변입니다. [ref:1]",
            "citations": [
                {
                    "ref": 1,
                    "section": first.section,
                    "slug": first.slug,
                }
            ],
            "schema": RESPONSE_SCHEMA,
            "schema_version": 1,
        }


class LocalAnswerRouteTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    docs: Path = Path()
    generator: FakeAnswerGenerator = cast(
        FakeAnswerGenerator,
        cast(object, None),
    )
    service: LocalAnswerService = cast(
        LocalAnswerService,
        cast(object, None),
    )

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="local-answer-")
        self.docs = Path(self.temporary.name) / "docs"
        papers = self.docs / "papers"
        paper = papers / "001_Alpha"
        topic = self.docs / "qa_fixture"
        paper.mkdir(parents=True)
        topic.mkdir()
        _ = (papers / "_papers_index.json").write_text(
            json.dumps(
                [
                    {
                        "slug": "001_Alpha",
                        "title": "Alpha",
                        "topics": ["qa_fixture"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        _ = (paper / "review.md").write_text(
            "\n".join(
                (
                    "# Alpha",
                    "",
                    "## Essence",
                    "a alpha agent 한국어",
                    "",
                    "## Achievement",
                    "local evidence",
                    "",
                )
            ),
            encoding="utf-8",
        )
        _ = build_sparse_index("qa_fixture", self.docs)
        self.generator = FakeAnswerGenerator()
        self.service = LocalAnswerService(self.docs, self.generator)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_contract_boundaries_and_schema_files_are_exact(self) -> None:
        self.assertEqual(MAX_BODY_BYTES, 8192)
        self.assertEqual(MAX_QUERY_CHARS, 2000)
        self.assertEqual(MAX_CONTEXT_CHARS, 24000)
        self.assertEqual(MAX_RESPONSE_BYTES, 32768)
        root = Path(__file__).resolve().parents[1]
        request = cast(
            dict[str, object],
            cast(
                object,
                json.loads(
                    (
                        root
                        / "schemas"
                        / "local-answer-request-v1.json"
                    ).read_text(encoding="utf-8")
                ),
            ),
        )
        response = cast(
            dict[str, object],
            cast(
                object,
                json.loads(
                    (
                        root
                        / "schemas"
                        / "local-answer-response-v1.json"
                    ).read_text(encoding="utf-8")
                ),
            ),
        )
        self.assertEqual(request["$id"], REQUEST_SCHEMA)
        self.assertEqual(response["$id"], RESPONSE_SCHEMA)

    def test_happy_answer_retrieves_bounded_review_evidence(self) -> None:
        response = self.service.answer(
            {
                "length": "short",
                "query": "alpha",
                "topic": "qa_fixture",
            }
        )
        self.assertEqual(response["schema"], RESPONSE_SCHEMA)
        citations = cast(
            list[dict[str, object]],
            cast(object, response["citations"]),
        )
        self.assertEqual(citations[0]["slug"], "001_Alpha")
        chunks = self.generator.calls[0][3]
        self.assertLessEqual(len(chunks), 8)
        self.assertLessEqual(sum(len(chunk.text) for chunk in chunks), 24000)

    def test_request_boundaries_and_typed_failures(self) -> None:
        for query in ("a", "alpha " + ("a" * 1994)):
            with self.subTest(size=len(query)):
                response = self.service.answer(
                    {
                        "length": "medium",
                        "query": query,
                        "topic": "qa_fixture",
                    }
                )
                self.assertEqual(response["schema"], RESPONSE_SCHEMA)
        for request, status in (
            (
                {
                    "length": "short",
                    "query": "a" * (MAX_QUERY_CHARS + 1),
                    "topic": "qa_fixture",
                },
                413,
            ),
            (
                {
                    "length": "invalid",
                    "query": "alpha",
                    "topic": "qa_fixture",
                },
                422,
            ),
            (
                {
                    "length": "short",
                    "query": " ",
                    "topic": "qa_fixture",
                },
                422,
            ),
            (
                {
                    "length": "short",
                    "query": "alpha",
                    "topic": "../escape",
                },
                422,
            ),
            (
                {
                    "length": "short",
                    "query": "alpha",
                    "topic": "missing",
                },
                404,
            ),
        ):
            with self.subTest(request=request):
                with self.assertRaises(LocalAnswerError) as raised:
                    _ = self.service.answer(request)
                self.assertEqual(raised.exception.status, status)

    def test_citation_and_output_mismatch_fail_closed(self) -> None:
        self.generator.response = {
            "answer": "잘못된 인용 [ref:9]",
            "citations": [
                {"ref": 9, "section": "Unknown", "slug": "999_Missing"}
            ],
            "schema": RESPONSE_SCHEMA,
            "schema_version": 1,
        }
        with self.assertRaises(LocalAnswerError) as raised:
            _ = self.service.answer(
                {
                    "length": "short",
                    "query": "alpha",
                    "topic": "qa_fixture",
                }
            )
        self.assertEqual(raised.exception.status, 502)

        sized = {
            "answer": "[ref:1]",
            "citations": [
                {"ref": 1, "section": "Essence", "slug": "001_Alpha"}
            ],
            "schema": RESPONSE_SCHEMA,
            "schema_version": 1,
        }
        base_size = len(
            (
                json.dumps(
                    sized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        remaining = MAX_RESPONSE_BYTES - base_size
        korean_count, ascii_count = divmod(remaining, 3)
        sized["answer"] = (
            ("한" * korean_count)
            + ("x" * ascii_count)
            + "[ref:1]"
        )
        self.generator.response = cast(
            dict[str, object],
            cast(object, sized),
        )
        exact = self.service.answer(
            {
                "length": "long",
                "query": "alpha",
                "topic": "qa_fixture",
            }
        )
        self.assertEqual(
            len(
                (
                    json.dumps(
                        exact,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            ),
            MAX_RESPONSE_BYTES,
        )
        sized["answer"] = str(sized["answer"]) + "x"
        with self.assertRaises(LocalAnswerError) as raised:
            _ = self.service.answer(
                {
                    "length": "long",
                    "query": "alpha",
                    "topic": "qa_fixture",
                }
            )
        self.assertEqual(raised.exception.status, 413)

    def _request(
        self,
        base_url: str,
        path: str,
        *,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        request = urllib.request.Request(
            base_url + path,
            data=body,
            method="POST" if body is not None else "GET",
            headers={"Content-Type": "application/json"},
        )
        try:
            response = cast(
                urllib.response.addinfourl,
                urllib.request.urlopen(request, timeout=5),
            )
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = cast(
                dict[str, object],
                cast(object, json.loads(response.read())),
            )
            return (
                cast(int, response.status),
                payload,
                dict(response.headers),
            )

    def test_real_listener_health_answer_limits_and_no_cors(self) -> None:
        server = create_server(
            "127.0.0.1",
            0,
            self.docs,
            self.service,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            base = f"http://{host}:{port}"
            status, health, headers = self._request(base, "/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)

            body = json.dumps(
                {
                    "length": "short",
                    "query": "alpha",
                    "topic": "qa_fixture",
                }
            ).encode()
            status, answer, headers = self._request(
                base,
                "/api/answer",
                body=body,
            )
            self.assertEqual(status, 200)
            self.assertEqual(answer["schema"], RESPONSE_SCHEMA)
            self.assertNotIn("Access-Control-Allow-Origin", headers)

            with socket.create_connection(
                (cast(str, host), port),
                timeout=5,
            ) as connection:
                connection.sendall(
                    "\r\n".join(
                        (
                            "POST /api/answer HTTP/1.1",
                            f"Host: {host}:{port}",
                            "Content-Type: application/json",
                            (
                                "Content-Length: "
                                f"{MAX_BODY_BYTES + 1}"
                            ),
                            "Expect: 100-continue",
                            "",
                            "",
                        )
                    ).encode("ascii")
                )
                oversized_raw = connection.recv(4096)
            self.assertTrue(
                oversized_raw.startswith(
                    b"HTTP/1.1 413 Request Entity Too Large"
                ),
                oversized_raw,
            )
            self.assertNotIn(b"100 Continue", oversized_raw)

            for denied in ("/api/embed", "/api/audio-email", "/api/citedby"):
                status, _, _ = self._request(base, denied, body=b"{}")
                self.assertEqual(status, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_real_probe_passes_against_listener(self) -> None:
        server = create_server(
            "127.0.0.1",
            0,
            self.docs,
            self.service,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "tools"
                        / "probe_local_answer.py"
                    ),
                    "--base-url",
                    server.public_url,
                    "--contract",
                    "v1",
                    "--include-boundaries",
                    "--docs-dir",
                    str(self.docs),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            report = cast(
                dict[str, object],
                cast(object, json.loads(completed.stdout)),
            )
            self.assertEqual(report["result"], "PASS")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_contention_is_409_and_non_loopback_bind_is_rejected(self) -> None:
        self.assertTrue(self.service.generation_lock.acquire(blocking=False))
        try:
            with self.assertRaises(LocalAnswerError) as raised:
                _ = self.service.answer(
                    {
                        "length": "short",
                        "query": "alpha",
                        "topic": "qa_fixture",
                    }
                )
            self.assertEqual(raised.exception.status, 409)
        finally:
            self.service.generation_lock.release()
        with self.assertRaises(ValueError):
            _ = create_server("0.0.0.0", 0, self.docs, self.service)
        with (
            patch.object(Path, "is_symlink", return_value=True),
            self.assertRaises(ValueError),
        ):
            _ = create_server(
                "127.0.0.1",
                0,
                self.docs,
                self.service,
            )

    def test_host_origin_schema_and_static_secret_guards_precede_work(
        self,
    ) -> None:
        secret = self.docs / "_local_keys.json"
        _ = secret.write_text('{"secret":"must-not-serve"}', encoding="utf-8")
        server = create_server(
            "127.0.0.1",
            0,
            self.docs,
            self.service,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = b'{"length":"short","query":"alpha","topic":"qa_fixture"}'

        def raw(
            path: str,
            headers: dict[str, str],
            payload: bytes = body,
            method: str = "POST",
        ) -> tuple[int, dict[str, object]]:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            try:
                connection.request(
                    method,
                    path,
                    body=payload if method == "POST" else None,
                    headers=headers,
                )
                response = connection.getresponse()
                value = cast(
                    dict[str, object],
                    cast(object, json.loads(response.read())),
                )
                return response.status, value
            finally:
                connection.close()

        try:
            status, _ = raw(
                "/api/answer",
                {
                    "Content-Type": "application/json",
                    "Host": "evil.example",
                    "Origin": server.public_url,
                    "X-CSRF-Token": server.csrf_token,
                },
            )
            self.assertEqual(status, 400)
            status, _ = raw(
                "/api/answer",
                {
                    "Content-Type": "application/json",
                    "Host": urlsplit(server.public_url).netloc,
                    "Origin": "http://evil.example",
                    "X-CSRF-Token": server.csrf_token,
                },
            )
            self.assertEqual(status, 403)
            status, _ = raw(
                "/api/answer",
                {
                    "Content-Type": "application/json",
                    "Host": urlsplit(server.public_url).netloc,
                    "Origin": server.public_url,
                },
            )
            self.assertEqual(status, 403)
            status, value = raw(
                "/api/answer",
                {
                    "Content-Type": "application/json",
                    "Host": urlsplit(server.public_url).netloc,
                },
                b"".join(
                    (
                        b'{"length":"short","query":"alpha",',
                        b'"topic":"qa_fixture","topic":"duplicate"}',
                    )
                ),
            )
            self.assertEqual(status, 400)
            self.assertEqual(value["status"], "json-invalid")
            status, _ = raw(
                "/api/answer",
                {
                    "Content-Type": "text/plain",
                    "Host": urlsplit(server.public_url).netloc,
                },
            )
            self.assertEqual(status, 415)
            status, _ = raw(
                "/api/answer",
                {"Host": urlsplit(server.public_url).netloc},
                b"",
                method="OPTIONS",
            )
            self.assertEqual(status, 404)
            for name in ("_local_keys.json", "_LOCAL_KEYS.JSON"):
                get = urllib.request.Request(
                    server.public_url + "/" + name,
                    method="GET",
                )
                with self.assertRaises(
                    urllib.error.HTTPError
                ) as raised:
                    response = cast(
                        urllib.response.addinfourl,
                        urllib.request.urlopen(get, timeout=5),
                    )
                    response.close()
                raised.exception.close()
                self.assertEqual(raised.exception.code, 404)
            self.assertEqual(self.generator.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_saved_auth_generation_is_cached_after_citation_validation(
        self,
    ) -> None:
        gateway_root = Path(self.temporary.name) / "gateway"
        gateway_root.mkdir()
        executable = gateway_root / "codex-fake.exe"
        _ = executable.write_bytes(b"signed-fake-codex")
        runner = FakeRunner()
        response = {
            "answer": "캐시된 답변 [ref:1]",
            "citations": [
                {"ref": 1, "section": "Essence", "slug": "001_Alpha"}
            ],
            "schema": RESPONSE_SCHEMA,
            "schema_version": 1,
        }
        with patch.dict(os.environ, {"PAPER_CURATION_TESTING": "1"}):
            gateway = CodexGateway.for_testing(
                GatewayPaths(
                    Path(__file__).resolve().parents[2],
                    executable,
                    gateway_root / "codex-resolved.json",
                    True,
                ),
                runner,
            )
            _ = gateway.requalify(accept=True)
            qualified_executions = sum(
                1
                for call in runner.calls
                if len(call.argv) > 1 and call.argv[1] == "exec"
            )
            runner.response = cast(
                RunnerJsonObject,
                cast(object, response),
            )
            generator = LocalAnswerCodex(
                gateway,
                RuntimePolicy("codex"),
                self.docs,
            )
            service = LocalAnswerService(self.docs, generator)
            request = {
                "length": "short",
                "query": "alpha",
                "topic": "qa_fixture",
            }
            first = service.answer(request)
            second = service.answer(request)
            successful_executions = sum(
                1
                for call in runner.calls
                if len(call.argv) > 1 and call.argv[1] == "exec"
            )
            cache_files = sorted(
                (self.docs / "qa_fixture" / ".llm_cache").glob("*.json")
            )
            runner.response = cast(
                RunnerJsonObject,
                cast(
                    object,
                    {
                        "answer": "잘못된 인용 [ref:9]",
                        "citations": [
                            {
                                "ref": 9,
                                "section": "Unknown",
                                "slug": "999_Missing",
                            }
                        ],
                        "schema": RESPONSE_SCHEMA,
                        "schema_version": 1,
                    },
                ),
            )
            with self.assertRaises(LocalAnswerError) as failed:
                _ = service.answer(
                    {
                        "length": "short",
                        "query": "agent",
                        "topic": "qa_fixture",
                    }
                )
            self.assertEqual(failed.exception.status, 502)
            self.assertEqual(
                sorted(
                    (self.docs / "qa_fixture" / ".llm_cache").glob("*.json")
                ),
                cache_files,
            )
        self.assertEqual(first, second)
        executions = [
            call
            for call in runner.calls
            if len(call.argv) > 1 and call.argv[1] == "exec"
        ]
        self.assertEqual(
            successful_executions - qualified_executions,
            1,
        )
        self.assertEqual(
            len(executions) - successful_executions,
            2,
        )
        answer_execution = executions[successful_executions - 1]
        model_index = answer_execution.argv.index("--model") + 1
        self.assertEqual(
            answer_execution.argv[model_index],
            "gpt-5.6-terra",
        )

    def test_cross_process_topic_lock_is_409(self) -> None:
        lock = TopicLock.acquire(
            self.docs / ".local-answer-locks" / "qa_fixture.lock",
            "qa_fixture",
        )
        try:
            with self.assertRaises(LocalAnswerError) as raised:
                _ = self.service.answer(
                    {
                        "length": "short",
                        "query": "alpha",
                        "topic": "qa_fixture",
                    }
                )
            self.assertEqual(raised.exception.status, 409)
        finally:
            lock.release()


if __name__ == "__main__":
    _ = unittest.main()
