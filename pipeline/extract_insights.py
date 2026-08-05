"""Generate retained paper connections; cross-category insights are explicit-off."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config_loader import (  # noqa: E402
    PAPERS_DIR as _PAPERS_DIR,
    get_topic_dir,
    load_config,
)
from pipeline.lib.generation_cache import GenerationCacheError  # noqa: E402
from pipeline.providers.codex_gateway import CodexGatewayError  # noqa: E402
from pipeline.runtime_policy import (  # noqa: E402
    RuntimePolicy,
    RuntimePolicyError,
    resolve_runtime_policy,
)
from pipeline.schemas.codex_schema import JsonObject  # noqa: E402

PAPERS_DIR = str(_PAPERS_DIR)


class ConnectionGenerationError(RuntimeError):
    """A retained connection batch could not be validated completely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def log(message: str) -> None:
    print(message, flush=True)


def load_topic_data(topic: str):
    """Load topic papers and multi-class category membership."""
    papers_path = Path(PAPERS_DIR) / "_papers_index.json"
    all_papers = json.loads(papers_path.read_text(encoding="utf-8"))
    topic_papers = [paper for paper in all_papers if topic in paper.get("topics", [])]
    category_papers = defaultdict(list)
    seen_in_category = defaultdict(set)
    for paper in topic_papers:
        classification = paper.get("classifications", {}).get(topic, {})
        categories = classification.get(
            "all_categories",
            [classification.get("primary_category", "Other")],
        )
        for category in categories:
            if paper["slug"] not in seen_in_category[category]:
                category_papers[category].append(paper)
                seen_in_category[category].add(paper["slug"])
    return topic_papers, category_papers


def _embed_model_tag(cache_path: Path) -> str | None:
    try:
        if cache_path.exists():
            value = json.loads(cache_path.read_text(encoding="utf-8"))
            tag = value.get("embed_model")
            if isinstance(tag, str) and tag:
                return tag
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    try:
        from pipeline.lib import specter2_embed
        return specter2_embed.EMBED_TAG
    except Exception:
        return None


def extract_paper_connections(
    topic,
    category_papers,
    generator,
    *,
    all_topic_papers=None,
    topic_dir=None,
    topic_slugs=None,
    seed_cache_only=False,
):
    """Generate every dirty source through strict cached long-form Codex only."""
    from pipeline.lib import conn_cache
    from pipeline.lib.connections import sync_topic_connections
    from pipeline.topic_modeling import (
        compute_embeddings,
        compute_related_candidates,
        extract_originalities,
        generate_connections_from_candidates,
    )

    target_slugs = {
        paper["slug"]
        for category, papers in category_papers.items()
        if category != "Other" and len(papers) >= 3
        for paper in papers
    }
    if not target_slugs:
        return {
            "status": "ok",
            "connections": {},
            "completed_slugs": [],
            "dirty_slugs": [],
        }

    pool = all_topic_papers or [
        paper for papers in category_papers.values() for paper in papers
    ]
    unique_pool = []
    seen = set()
    for paper in pool:
        if paper["slug"] not in seen:
            unique_pool.append(paper)
            seen.add(paper["slug"])
    pool = unique_pool

    originalities = extract_originalities(pool)
    directory = Path(topic_dir) if topic_dir is not None else None
    cache_path = directory / "_embeddings_cache.json" if directory else None
    embeddings, slugs = compute_embeddings(
        originalities,
        str(cache_path) if cache_path is not None else None,
    )
    top_n = int(os.environ.get("EXTRACT_INSIGHTS_TOPN_CAND", "25"))
    candidates = compute_related_candidates(
        embeddings,
        slugs,
        top_k=top_n,
    )
    candidates = {
        slug: rows
        for slug, rows in candidates.items()
        if slug in target_slugs
    }
    embed_tag = _embed_model_tag(cache_path) if cache_path is not None else None
    previous_cache = (
        conn_cache.load_topk_cache(str(directory), top_n, scope="ei")
        if directory is not None
        else {}
    )
    if seed_cache_only:
        if directory is not None:
            conn_cache.save_topk_cache(
                str(directory),
                candidates,
                top_n,
                embed_tag,
                scope="ei",
            )
        return {
            "status": "seeded",
            "connections": {},
            "completed_slugs": [],
            "dirty_slugs": [],
        }

    existing = {}
    connection_path = directory / "_paper_connections.json" if directory else None
    if connection_path is not None and connection_path.exists():
        try:
            decoded = json.loads(connection_path.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                existing = decoded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = {}
    incremental = os.environ.get("CONN_INCREMENTAL", "1").strip().lower() not in {
        "0", "off", "false", "no",
    }
    full_rebuild = os.environ.get("CONN_FULL_REBUILD", "").strip().lower() in {
        "1", "on", "true", "yes",
    }
    dirty, reason = conn_cache.compute_dirty(
        candidates,
        previous_cache,
        existing,
        top_n,
        embed_tag,
        force=full_rebuild or not incremental,
        log=log,
    )
    generation_candidates = conn_cache.restrict_candidates(candidates, dirty)
    priority = {slug for slug in generation_candidates if not existing.get(slug)}
    if generation_candidates:
        connections, completed = generate_connections_from_candidates(
            generation_candidates,
            pool,
            generator,
            topic,
            batch_size=int(os.environ.get("EXTRACT_INSIGHTS_CONN_BATCH", "15")),
            priority_slugs=priority,
        )
        missing = set(generation_candidates) - set(completed)
        if missing:
            raise ConnectionGenerationError(
                "generation-incomplete",
                f"{len(missing)} source papers lacked a validated decision",
            )
    else:
        connections = {}
        completed = set()

    if directory is not None and topic_slugs is not None:
        sync_topic_connections(
            connections,
            topic,
            topic_slugs,
            str(directory),
            log=log,
        )
        next_sets = conn_cache.next_cache_sets(
            candidates,
            previous_cache,
            dirty,
            set(completed),
        )
        conn_cache.save_topk_cache(
            str(directory),
            candidates,
            top_n,
            embed_tag,
            scope="ei",
            sets=next_sets,
        )
    return {
        "status": "ok",
        "connections": connections,
        "completed_slugs": sorted(completed),
        "dirty_slugs": sorted(dirty),
        "reason": reason,
    }


def _run_insights(
    topic="ai4s",
    *,
    insights_only=False,
    connections_only=True,
    categories=None,
    seed_cache_only=False,
    runtime_policy=None,
    generator=None,
):
    """Run retained connections only; every cross-insight request is denied."""
    if insights_only or not connections_only:
        return {
            "status": "policy_denied",
            "reason": "cross-category-insights-explicit-off",
        }
    policy = runtime_policy or resolve_runtime_policy(
        cast(JsonObject, cast(object, load_config()))
    )
    if policy.mode != "codex" and generator is None:
        return {"status": "policy_denied", "reason": "runtime-off"}
    topic_dir = Path(get_topic_dir(topic))
    topic_papers, category_papers = load_topic_data(topic)
    if categories:
        selected = set(categories)
        category_papers = {
            category: papers
            for category, papers in category_papers.items()
            if category in selected
        }
    if generator is None:
        from pipeline.topic_modeling import TopicCodex
        generator = TopicCodex.production(topic_dir, policy)
    try:
        result = extract_paper_connections(
            topic,
            category_papers,
            generator,
            all_topic_papers=topic_papers,
            topic_dir=topic_dir,
            topic_slugs=[paper["slug"] for paper in topic_papers],
            seed_cache_only=seed_cache_only,
        )
    except (
        ConnectionGenerationError,
        GenerationCacheError,
        CodexGatewayError,
    ) as error:
        code = getattr(error, "code", "connection-generation-failed")
        return {"status": "failed", "reason": str(code)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate retained paper connections; cross insights are disabled"
    )
    _ = parser.add_argument("--topic", default="ai4s")
    _ = parser.add_argument("--insights-only", action="store_true")
    _ = parser.add_argument("--connections-only", action="store_true")
    _ = parser.add_argument(
        "--only",
        choices=["connections", "insights", "all"],
        default="connections",
    )
    _ = parser.add_argument("--categories", nargs="+")
    _ = parser.add_argument("--seed-cache-only", action="store_true")
    _ = parser.add_argument("--llm-mode", default=None)
    args = parser.parse_args()
    insights_requested = args.insights_only or args.only == "insights"
    connections_requested = (
        args.connections_only or args.only in {"connections", "all"}
    )
    try:
        policy = resolve_runtime_policy(
            cast(JsonObject, cast(object, load_config())),
            args.llm_mode,
        )
        result = _run_insights(
            topic=args.topic,
            insights_only=insights_requested,
            connections_only=connections_requested,
            categories=args.categories,
            seed_cache_only=args.seed_cache_only,
            runtime_policy=policy,
        )
    except RuntimePolicyError as error:
        print(f"Runtime policy denied: {error.code}", file=sys.stderr)
        return 2
    if result.get("status") not in {"ok", "seeded"}:
        print(f"Connection stage failed: {result.get('reason', 'unknown')}")
        return 2
    print(
        f"Paper connections: completed={len(result.get('completed_slugs', []))} "
        f"dirty={len(result.get('dirty_slugs', []))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
