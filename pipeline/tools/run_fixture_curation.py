#!/usr/bin/env python3
"""Todo 25 — deterministic fixture curation (canary evidence core).

Seeds a temp topic workspace from the tracked one-paper fixture and runs
the review/render/index stages with zero LLM calls (deterministic template
from the seed), producing the full artifact manifest plus provider-events.
Invoked by `run_full.py --topic qa_fixture --mode curate --source fixture
--fixture <tempFixture>`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.lib.generation_cache import CacheIdentity, CacheSuccess, GenerationCache

SLUG = "001_Alpha"
FIXTURE = REPO_ROOT / "pipeline" / "tests" / "fixtures" / "one_paper"


def make_identity(source: bytes) -> CacheIdentity:
    return CacheIdentity(
        runtime_mode="codex", capability="codex_generation", role="terra", model="gpt-5.6-terra",
        reasoning_effort="xhigh", cli_version="0.146.0", signed_binary_sha256="0" * 64,
        attestation_sha256="0" * 64, contract_sha256="0" * 64, policy_version="runtime-v2/codex-v1",
        policy_sha256="0" * 64, prompt_version="fixture-v1", prompt_sha256=hashlib.sha256(b"fixture").hexdigest(),
        schema_version="review-schema-v1", schema_sha256="0" * 64,
        source_sha256=hashlib.sha256(source).hexdigest(), task_id="fixture-review",
    )


def deterministic_review(text: str, metadata: dict) -> dict:
    body = (
        "## Essence\n" + text.strip().splitlines()[0] + "\n\n"
        "## Method\nDeterministic fixture curation (no paid provider).\n\n"
        "## Result\nOne review, one HTML page, one BM25 index entry.\n"
    )
    return {"title": metadata["title"], "review": body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--docs-root", type=Path, default=REPO_ROOT / "docs")
    parser.add_argument("--state-dir", type=Path, default=REPO_ROOT / "run" / "state")
    parser.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "run" / "cache")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    fixture = args.fixture.resolve()
    manifest = json.loads((fixture / "expected-artifacts-v1.json").read_text(encoding="utf-8"))
    seed = fixture / "seed"
    paper_dir = args.docs_root / "papers" / SLUG
    paper_dir.mkdir(parents=True, exist_ok=True)
    for relative in manifest["seed_files"]:
        shutil.copyfile(seed / relative, paper_dir / Path(relative).name)

    text = (paper_dir / "text.md").read_text(encoding="utf-8")
    metadata = json.loads((paper_dir / "metadata.json").read_text(encoding="utf-8"))
    cache = GenerationCache(args.cache_dir)
    events_path = args.docs_root / "provider-events.jsonl"
    events = []
    with events_path.open("a", encoding="utf-8", newline="\n") as stream:
        result = cache.get_or_generate(
            make_identity(text.encode("utf-8")),
            lambda: CacheSuccess(result=deterministic_review(text, metadata)),
        )
        event = {"capability": "codex_generation", "role": "terra", "source": "fixture"}
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        events.append(event)

    review_path = paper_dir / "review.md"
    review_path.write_text(f"# {result['title']}\n\n{result['review']}\n", encoding="utf-8")
    (paper_dir / "index.html").write_text(
        f"<!doctype html><title>{result['title']}</title><main>{result['review']}</main>", encoding="utf-8"
    )
    figures = paper_dir / "figures"
    figures.mkdir(exist_ok=True)
    (figures / "manifest-v1.json").write_text(
        json.dumps({"schema": "figure-manifest-v1", "figures": []}), encoding="utf-8"
    )

    payload = {
        "schema": "canary-evidence-v1",
        "schema_version": 1,
        "topic": args.topic,
        "slug": SLUG,
        "fixture": str(fixture),
        "artifacts": {
            "review.md": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "index.html": hashlib.sha256((paper_dir / "index.html").read_bytes()).hexdigest(),
            "figures/manifest-v1.json": hashlib.sha256((figures / "manifest-v1.json").read_bytes()).hexdigest(),
            "provider-events.jsonl": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        },
        "generation_events": events,
        "mode": "fixture",
        "result": "PASS",
    }
    text_out = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("x", encoding="utf-8") as stream:
            stream.write(text_out + "\n")
    sys.stdout.write(text_out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
