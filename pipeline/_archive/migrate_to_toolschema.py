"""
Phase 3 migration: rewrite every ``docs/papers/{slug}/review.md`` with
schema v1 YAML frontmatter (Obsidian Properties-compatible).

Workflow:
  1. Snapshot the existing review.md to ``docs/papers/.legacy/{slug}_v0.md``.
  2. For each topic in priority order, run ``inject_frontmatter`` so
     review.md gets a fresh v1 frontmatter built from
     ``_papers_index.json`` (the single source of truth).
  3. Multi-topic papers: the first topic that lists the paper writes the
     frontmatter; classification fields come from the paper's
     ``primary_topic``, and tags are emitted for every topic, so the
     order only matters for the audit log.

Usage:
  PYTHONUTF8=1 python pipeline/migrate_to_toolschema.py             # dry-run
  PYTHONUTF8=1 python pipeline/migrate_to_toolschema.py --execute   # run
  PYTHONUTF8=1 python pipeline/migrate_to_toolschema.py --execute --topic ai4s  # one topic
  PYTHONUTF8=1 python pipeline/migrate_to_toolschema.py --execute --skip-backup

After migration ``build_papers_index`` will read scores/category from
the v1 frontmatter and skip the legacy body-regex path, which removes
~150 lines of fragile parsing from the hot path.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPERS_DIR = PROJECT_ROOT / "docs" / "papers"
PIPELINE = PROJECT_ROOT / "pipeline"
LEGACY_DIR = PAPERS_DIR / ".legacy"

# Topic priority: papers that appear in multiple topics get processed by
# the first topic in this list. Single-topic papers don't care about
# ordering.
TOPIC_PRIORITY = ["ai4s", "scisci", "humanoid", "physical-ai", "ai4s+scisci"]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def backup_all_reviews() -> int:
    """Copy every existing review.md to docs/papers/.legacy/{slug}_v0.md.
    Returns the number of files snapshotted. Idempotent: skips slugs
    whose backup already exists.
    """
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for slug_dir in sorted(PAPERS_DIR.iterdir()):
        if not slug_dir.is_dir() or not slug_dir.name[0:1].isdigit():
            continue
        review = slug_dir / "review.md"
        if not review.exists():
            continue
        backup = LEGACY_DIR / f"{slug_dir.name}_v0.md"
        if backup.exists():
            continue
        shutil.copy2(review, backup)
        count += 1
    return count


def discover_topics() -> list[str]:
    """Topics that actually have papers in the index."""
    import json
    idx_path = PAPERS_DIR / "_papers_index.json"
    if not idx_path.exists():
        log(f"  WARNING: {idx_path} missing")
        return []
    with open(idx_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    seen = set()
    for p in idx:
        for t in p.get("topics", []) or []:
            seen.add(t)
    ordered = [t for t in TOPIC_PRIORITY if t in seen]
    extra = sorted(seen - set(ordered))
    return ordered + extra


def run_inject_frontmatter(topic: str) -> int:
    """Invoke ``inject_frontmatter.py --topic {topic} --skip-zotero``."""
    cmd = [sys.executable, "-u", str(PIPELINE / "inject_frontmatter.py"),
           "--topic", topic, "--skip-zotero"]
    log(f"  $ {' '.join(cmd)}")
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser(description="Migrate review.md to schema v1 frontmatter")
    ap.add_argument("--execute", action="store_true",
                    help="Actually run. Default is dry-run.")
    ap.add_argument("--topic", action="append",
                    help="Restrict to one or more topics (default: all topics with papers).")
    ap.add_argument("--skip-backup", action="store_true",
                    help="Skip backup step (use only if a backup already exists).")
    args = ap.parse_args()

    log(f"Migration: review.md → schema v1 frontmatter")
    log(f"  Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    log(f"  Backup dir: {LEGACY_DIR}")

    topics = args.topic or discover_topics()
    log(f"  Topics ({len(topics)}): {', '.join(topics)}")

    # Count how many review.md exist
    review_count = sum(
        1 for d in PAPERS_DIR.iterdir()
        if d.is_dir() and d.name[0:1].isdigit() and (d / "review.md").exists()
    )
    log(f"  review.md files on disk: {review_count}")

    if not args.execute:
        log("\n(dry-run) Run with --execute to apply.")
        log("  Steps that would run:")
        log(f"    1. Backup {review_count} review.md → {LEGACY_DIR}")
        for t in topics:
            log(f"    2. inject_frontmatter --topic {t} --skip-zotero")
        return

    # 1. Backup
    if args.skip_backup:
        log("\n[skip-backup] Backup step skipped.")
    else:
        log(f"\n[1/2] Backing up review.md → {LEGACY_DIR.relative_to(PROJECT_ROOT)}/...")
        n_backed = backup_all_reviews()
        log(f"  Snapshotted {n_backed} files (existing backups preserved).")

    # 2. inject_frontmatter per topic
    log(f"\n[2/2] Re-injecting v1 frontmatter for {len(topics)} topics...")
    failures = []
    for t in topics:
        log(f"\n  ── {t} ──")
        rc = run_inject_frontmatter(t)
        if rc != 0:
            failures.append((t, rc))
            log(f"  FAIL: inject_frontmatter --topic {t} exited {rc}")

    if failures:
        log(f"\nFailed topics: {failures}")
        sys.exit(1)
    log("\nMigration done.")


if __name__ == "__main__":
    main()
