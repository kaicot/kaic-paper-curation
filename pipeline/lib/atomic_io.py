"""
Atomic write helpers — `*.tmp + os.replace()` pattern.

Use everywhere we serialize index/summary/narrative JSON to avoid partial
writes corrupting the source-of-truth files when a process is killed mid-flush.
"""

import json
import os
from pathlib import Path


def atomic_write_text(path, text, encoding="utf-8"):
    """Write `text` to `path` atomically. Crash anywhere = old file intact."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
    return p


def atomic_write_json(path, obj, indent=2, ensure_ascii=False):
    return atomic_write_text(
        path, json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
    )


def atomic_write_bytes(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p
