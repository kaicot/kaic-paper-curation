"""Unavailable secondary capability boundary: generate-workflow."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Never

_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pipeline").is_dir()
)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.secondary_capability_guard import (  # noqa: E402
    CAPABILITY_STATUS as SECONDARY_CAPABILITY_STATUS,
    cli as _capability_cli,
    deny as _deny,
)

CAPABILITY_NAME = "generate-workflow"


def _unavailable(*_args: object, **_kwargs: object) -> Never:
    _ = (_args, _kwargs)
    return _deny(CAPABILITY_NAME)


def __getattr__(name: str):
    if name.startswith("__"):
        raise AttributeError(name)
    return _unavailable


def main(argv: list[str] | None = None) -> int:
    return _capability_cli(CAPABILITY_NAME, argv)


if __name__ == "__main__":
    raise SystemExit(main())
