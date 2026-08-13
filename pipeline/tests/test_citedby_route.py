"""Regression coverage for the retired local cited-by streaming route."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import cast, override

from pipeline.local_answer_service import (
    EvidenceChunk,
    LocalAnswerService,
)
from pipeline.serve_local import create_server
from pipeline.serve_local import LocalAnswerHTTPServer


class _UnusedGenerator:
    calls: int = 0

    def generate(
        self,
        *,
        topic: str,
        query: str,
        length: str,
        chunks: list[EvidenceChunk],
    ) -> dict[str, object]:
        del topic, query, length, chunks
        self.calls += 1
        raise AssertionError("retired cited-by route dispatched generation")


class CitedbyRouteDenialTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    generator: _UnusedGenerator = cast(
        _UnusedGenerator,
        cast(object, None),
    )
    server: LocalAnswerHTTPServer = cast(
        LocalAnswerHTTPServer,
        cast(object, None),
    )
    thread: threading.Thread = cast(
        threading.Thread,
        cast(object, None),
    )

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="citedby-route-denial-"
        )
        docs = Path(self.temporary.name) / "docs"
        docs.mkdir()
        self.generator = _UnusedGenerator()
        self.server = create_server(
            "127.0.0.1",
            0,
            docs,
            LocalAnswerService(docs, self.generator),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    @override
    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_citedby_post_is_json_404_without_dispatch_or_cors(self) -> None:
        request = urllib.request.Request(
            self.server.public_url + "/api/citedby",
            data=b'{"doi":"10.1/example"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            response = cast(
                urllib.response.addinfourl,
                urllib.request.urlopen(request, timeout=5),
            )
            response.close()
        error = raised.exception
        try:
            self.assertEqual(error.code, 404)
            payload = cast(
                dict[str, object],
                cast(object, json.loads(error.read())),
            )
            self.assertEqual(payload["status"], "not-found")
            self.assertNotIn(
                "Access-Control-Allow-Origin",
                error.headers,
            )
        finally:
            error.close()
        self.assertEqual(self.generator.calls, 0)

    def test_retired_streaming_route_never_writes_artifacts(self) -> None:
        root = Path(self.temporary.name) / "docs"
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        request = urllib.request.Request(
            self.server.public_url + "/api/citedby",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = cast(
                urllib.response.addinfourl,
                urllib.request.urlopen(request, timeout=5),
            )
            response.close()
        except urllib.error.HTTPError as error:
            error.close()
        after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(after, before)


if __name__ == "__main__":
    _ = unittest.main()
