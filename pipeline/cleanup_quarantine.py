"""Retired standalone quarantine cleanup entrypoint.

Search-index restore and purge remain available only through the journal-bound
``build_search_index.py`` interface.  This historical standalone name is kept
default-denied so operator scripts fail closed.
"""

from __future__ import annotations

import argparse
from typing import cast

from pipeline.release_dry_run import build_dry_run_plan, emit


SECONDARY_CAPABILITY_STATUS = "default-denied"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--topic", default="qa_fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = cast(bool, args.dry_run)
    topic = cast(str, args.topic)
    if dry_run:
        emit(
            build_dry_run_plan(
                entrypoint="pipeline/cleanup_quarantine.py",
                topic=topic,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
