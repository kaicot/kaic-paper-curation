"""
PDF-review 오매칭 복구 스크립트 (Phase 3).

`audit_matching.py`가 만든 `_audit_report.json`의 high-confidence 슬러그를 대상으로
review.md / text.md / figures/ 를 삭제하고, 뒤이어 실행할 재리뷰 명령을 출력한다.

안전장치:
  - 기본 --dry-run (실제 삭제 안 함)
  - --execute 로만 실제 삭제
  - 삭제 대상 슬러그를 `docs/{topic}/_fix_matching_backup_<ts>.json` 에 덤프 (복구용)
  - 다른 토픽을 공유(papers/*/has multiple topics) 하는 슬러그는 삭제하지 않고 건너뜀

Usage:
  # 기본: dry-run 으로 영향 범위 확인
  PYTHONUTF8=1 python pipeline/fix_matching.py --topic ai4s

  # 실제 삭제 + 재리뷰 명령 출력
  PYTHONUTF8=1 python pipeline/fix_matching.py --topic ai4s --execute

  # 특정 슬러그만 처리 (audit 결과와 관계없이)
  PYTHONUTF8=1 python pipeline/fix_matching.py --topic ai4s --slugs 088,1093 --execute
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from config_loader import PROJECT_ROOT, get_topic_dir

DOCS = PROJECT_ROOT / "docs"
PAPERS_DIR = DOCS / "papers"


def load_audit_report(topic):
    p = get_topic_dir(topic) / "_audit_report.json"
    if not p.exists():
        print(f"ERROR: {p} missing — run audit_matching.py first", file=sys.stderr)
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def load_index():
    p = PAPERS_DIR / "_papers_index.json"
    return json.loads(p.read_text(encoding="utf-8"))


def slug_has_other_topics(slug, index, this_topic):
    for e in index:
        if e.get("slug") == slug:
            topics = e.get("topics", [])
            return len(topics) > 1 and this_topic in topics
    return False


def delete_slug_artifacts(slug, dry_run=True):
    """Remove review.md / text.md / index.html / figures/. Keep _papers_index.json entry."""
    slug_dir = PAPERS_DIR / slug
    targets = []
    for name in ("review.md", "text.md", "index.html"):
        p = slug_dir / name
        if p.exists():
            targets.append(p)
    fig_dir = slug_dir / "figures"
    if fig_dir.is_dir():
        targets.append(fig_dir)

    if not dry_run:
        for t in targets:
            if t.is_dir():
                shutil.rmtree(t)
            else:
                t.unlink()
    return [str(t.relative_to(PAPERS_DIR)) for t in targets]


def _run_fix_matching(topic, *, slugs=None, execute=False, include_medium=False):
    """Programmatic entrypoint for fix_matching.

    `slugs` may be a list of slug-prefixes or a comma-separated string.
    """
    if isinstance(slugs, str):
        slugs_str = slugs
    elif slugs:
        slugs_str = ",".join(slugs)
    else:
        slugs_str = ""

    index = load_index()

    if slugs_str:
        prefixes = [s.strip() for s in slugs_str.split(",") if s.strip()]
        target_slugs = []
        for d in PAPERS_DIR.iterdir():
            if d.is_dir() and any(d.name.startswith(p) for p in prefixes):
                target_slugs.append(d.name)
        source = f"--slugs ({len(prefixes)} prefixes)"
    else:
        report = load_audit_report(topic)
        levels = {"high"}
        if include_medium:
            levels.add("medium")
        target_slugs = [r["slug"] for r in report.get("results", [])
                        if r.get("confidence") in levels]
        source = f"_audit_report.json ({','.join(sorted(levels))})"

    if not target_slugs:
        print("No target slugs.")
        return

    skipped_shared = []
    fixable = []
    for slug in target_slugs:
        if slug_has_other_topics(slug, index, topic):
            skipped_shared.append(slug)
        else:
            fixable.append(slug)

    print(f"Source         : {source}")
    print(f"Target slugs   : {len(target_slugs)}")
    print(f"Fixable        : {len(fixable)}")
    print(f"Skipped(shared): {len(skipped_shared)}  (belong to other topics too)")
    print(f"Mode           : {'EXECUTE' if execute else 'DRY-RUN'}")
    print()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = {
        "timestamp": ts,
        "topic": topic,
        "source": source,
        "target_slugs": target_slugs,
        "fixable": fixable,
        "skipped_shared": skipped_shared,
    }
    backup_path = get_topic_dir(topic) / f"_fix_matching_backup_{ts}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    from lib.atomic_io import atomic_write_json
    atomic_write_json(backup_path, backup)
    print(f"Backup list    : {backup_path}")

    deleted_counts = 0
    for slug in fixable:
        targets = delete_slug_artifacts(slug, dry_run=not execute)
        print(f"  {'DEL' if execute else 'would-del'}: {slug}  "
              f"({len(targets)} items)")
        deleted_counts += len(targets)

    print()
    print(f"Total artifacts: {deleted_counts}")
    print()
    print("Next step — re-review the cleaned slugs:")
    prefixes = sorted({s.split("_", 1)[0] for s in fixable})
    chunks = [",".join(prefixes[i:i + 30]) for i in range(0, len(prefixes), 30)]
    for i, c in enumerate(chunks, 1):
        tag = f" (batch {i}/{len(chunks)})" if len(chunks) > 1 else ""
        print(f"  PYTHONUTF8=1 python pipeline/run_update_force.py "
              f"--topic {topic} --slugs {c} --strict-pdf{tag}")
    print()
    print("After re-review:")
    print(f"  PYTHONUTF8=1 python pipeline/audit_matching.py --topic {topic}")
    print("  (verify 'high-confidence mismatch' has dropped)")
    return {"fixable": fixable, "skipped_shared": skipped_shared,
            "deleted_artifacts": deleted_counts, "backup_path": str(backup_path)}


def main():
    ap = argparse.ArgumentParser(description="PDF-review mismatch recovery")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--execute", action="store_true",
                    help="Actually delete artifacts. Default is dry-run.")
    ap.add_argument("--slugs", default="",
                    help="Comma-separated slug prefixes to fix, overrides audit report selection.")
    ap.add_argument("--include-medium", action="store_true",
                    help="Also fix medium-confidence entries (default: high only).")
    args = ap.parse_args()
    _run_fix_matching(topic=args.topic, slugs=args.slugs,
                       execute=args.execute, include_medium=args.include_medium)


if __name__ == "__main__":
    main()
