"""Versioned constants shared by the loopback answer server and probe."""

from __future__ import annotations

import re
from pathlib import Path

MAX_BODY_BYTES = 8_192
MAX_QUERY_CHARS = 2_000
MAX_RETRIEVED_CHUNKS = 8
MAX_CONTEXT_CHARS = 24_000
MAX_CITATIONS = 12
MAX_RESPONSE_BYTES = 32_768

REQUEST_SCHEMA = "local-answer-request-v1"
RESPONSE_SCHEMA = "local-answer-response-v1"
ERROR_SCHEMA = "local-answer-error-v1"
READY_SCHEMA = "local-answer-ready-v1"
CONTRACT_VERSION = "v1"

TOPIC_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
TOPIC_RE = re.compile(TOPIC_PATTERN)
LENGTH_VALUES = frozenset({"short", "medium", "long"})

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
REQUEST_SCHEMA_PATH = SCHEMA_DIR / "local-answer-request-v1.json"
RESPONSE_SCHEMA_PATH = SCHEMA_DIR / "local-answer-response-v1.json"
