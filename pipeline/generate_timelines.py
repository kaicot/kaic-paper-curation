"""Generate strict cached text timeline narratives; images are skipped by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import cast, final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config_loader import (  # noqa: E402
    PAPERS_DIR as _PAPERS_DIR,
    get_topic_dir,
    load_config,
)
from pipeline.lib.atomic_io import atomic_write_json  # noqa: E402
from pipeline.lib.generation_cache import (  # noqa: E402
    CacheFailure,
    CacheIdentity,
    CacheSuccess,
    GenerationCache,
    GenerationCacheError,
)
from pipeline.providers.codex_gateway import CodexGateway, CodexGatewayError  # noqa: E402
from pipeline.runtime_policy import (  # noqa: E402
    RuntimePolicy,
    RuntimePolicyError,
    resolve_runtime_policy,
)
from pipeline.schemas.codex_schema import (  # noqa: E402
    JsonObject,
    SchemaError,
    validate_json,
)

PAPERS_DIR = str(_PAPERS_DIR)
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
with (SCHEMA_DIR / "timeline-category-v1.json").open("r", encoding="utf-8") as _handle:
    CATEGORY_SCHEMA = cast(JsonObject, json.load(_handle))
with (SCHEMA_DIR / "timeline-executive-v1.json").open("r", encoding="utf-8") as _handle:
    EXECUTIVE_SCHEMA = cast(JsonObject, json.load(_handle))


class TimelineGenerationError(RuntimeError):
    """A timeline result failed strict schema or semantic validation."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def log(message: str) -> None:
    print(message, flush=True)


def _validate_category(value: JsonObject, category: str, paper_count: int) -> JsonObject:
    validate_json(value, CATEGORY_SCHEMA)
    if value.get("category") != category:
        raise TimelineGenerationError("category mismatch")
    if value.get("paper_count") != paper_count:
        raise TimelineGenerationError("paper count mismatch")
    themes = value.get("sub_themes")
    if not isinstance(themes, list):
        raise TimelineGenerationError("sub_themes must be a list")
    names = [row.get("name") for row in themes if isinstance(row, dict)]
    if len(names) != len(set(names)):
        raise TimelineGenerationError("sub-theme names must be unique")
    return value


def _validate_executive(value: JsonObject) -> JsonObject:
    validate_json(value, EXECUTIVE_SCHEMA)
    text = value.get("executive_summary_ko")
    if not isinstance(text, str):
        raise TimelineGenerationError("executive summary must be text")
    korean = len([character for character in text if "가" <= character <= "힣"])
    if korean < len(text) * 0.3:
        raise TimelineGenerationError("executive summary Korean ratio too low")
    if not text.rstrip().endswith((".", "다.")):
        raise TimelineGenerationError("executive summary must end with a sentence")
    return value


@final
class TimelineCodex:
    """Long-form schema generation with success-only cache publication."""

    def __init__(self, runtime_policy, gateway, cache, identity_factory=CacheIdentity.from_gateway):
        self.runtime_policy = runtime_policy
        self.gateway = gateway
        self.cache = cache
        self.identity_factory = identity_factory

    @classmethod
    def production(cls, topic_dir: str | Path, runtime_policy: RuntimePolicy) -> "TimelineCodex":
        if runtime_policy.mode != "codex":
            raise GenerationCacheError(
                "policy-denied",
                "timeline generation requires codex mode",
            )
        return cls(
            runtime_policy,
            CodexGateway.production(ROOT),
            GenerationCache(Path(topic_dir) / ".llm_cache"),
        )

    def generate(
        self,
        *,
        prompt: str,
        schema: JsonObject,
        schema_version: str,
        prompt_version: str,
        source: JsonObject,
        task_id: str,
        validator,
    ) -> JsonObject:
        identity = self.identity_factory(
            runtime_policy=self.runtime_policy,
            gateway=self.gateway,
            role="long_form",
            prompt_version=prompt_version,
            prompt=prompt,
            schema_version=schema_version,
            schema=schema,
            source=_canonical(source),
            task_id=task_id,
        )

        def produce():
            try:
                candidate = self.gateway.generate_json("long_form", prompt, schema)
                return CacheSuccess(validator(candidate))
            except (
                CodexGatewayError,
                SchemaError,
                TimelineGenerationError,
                TypeError,
                ValueError,
            ):
                return CacheFailure("failed")

        result = self.cache.get_or_generate(identity, produce)
        return validator(result)


def _category_source(topic: str, category: str, papers) -> JsonObject:
    sub_counts = Counter(str(paper.get("sub_category", "General")) for paper in papers)
    rows = [
        {
            "essence": str(paper.get("essence", ""))[:200],
            "slug": str(paper.get("slug", "")),
            "sub_category": str(paper.get("sub_category", "")),
            "title": str(paper.get("title", ""))[:80],
            "year": str(paper.get("year", "")),
        }
        for paper in sorted(
            papers,
            key=lambda item: (
                str(item.get("year", "9999")),
                str(item.get("slug", "")),
            ),
        )
    ]
    return cast(JsonObject, cast(object, {
        "category": category,
        "paper_count": len(papers),
        "papers": rows,
        "sub_categories": [
            {"name": name, "paper_count": count}
            for name, count in sorted(sub_counts.items())
        ],
        "topic": topic,
    }))


def _category_prompt(source: JsonObject) -> str:
    return f"""Analyze this category's chronological research development:
{json.dumps(source, ensure_ascii=False, sort_keys=True)}

Return only JSON matching the supplied schema. Identify evidence-grounded sub-themes,
milestones, interactions, emergence/decline events, and the current state. Use English
for structured timeline labels and concise complete prose. Never invent paper counts."""


def _executive_prompt(topic: str, categories: list[JsonObject]) -> str:
    return f"""Write the Korean executive summary for the text-only research timeline of {topic}.
Category narratives:
{json.dumps(categories, ensure_ascii=False, sort_keys=True)}

Return only JSON matching the supplied schema. Write 800-1500 Korean characters in one
complete paragraph, explaining chronological development, recent trends, and future direction."""


def _entry_is_current(entry: object, input_hash: str) -> bool:
    if not isinstance(entry, dict) or entry.get("source_sha256") != input_hash:
        return False
    clean = cast(
        JsonObject,
        {
            key: value
            for key, value in entry.items()
            if not key.startswith("_") and key != "source_sha256"
        },
    )
    category = clean.get("category")
    count = clean.get("paper_count")
    if not isinstance(category, str) or not isinstance(count, int):
        return False
    try:
        _ = _validate_category(clean, category, count)
    except (SchemaError, TimelineGenerationError, TypeError, ValueError):
        return False
    return True


def _timeline_artifact(executive_summary: str, categories: list[JsonObject]) -> JsonObject:
    analyses = {
        str(category.get("category", "")): {
            "current_state_summary": category.get("current_state_summary", ""),
            "sub_themes": category.get("sub_themes", []),
        }
        for category in categories
        if category.get("category")
    }
    return cast(JsonObject, cast(object, {
        "category_analyses": analyses,
        "executive_summary_ko": executive_summary,
    }))


def _run_timeline(
    topic="ai4s",
    *,
    candidates=3,
    narrative_only=False,
    images_only=False,
    main_only=False,
    category_only=False,
    categories=None,
    mode="all",
    force_narrative=False,
    images="skip",
    runtime_policy=None,
    generator=None,
):
    """Generate complete text artifacts; image generation is a separate later stage."""
    _ = (candidates, narrative_only, main_only, category_only)
    if images_only or images == "generate" or mode == "images":
        return {"status": "unavailable", "reason": "image-generation-not-in-text-stage"}
    policy = runtime_policy or resolve_runtime_policy(
        cast(JsonObject, cast(object, load_config()))
    )
    if policy.mode != "codex" and generator is None:
        return {"status": "policy_denied", "reason": "runtime-off"}
    topic_dir = Path(get_topic_dir(topic))
    category_path = topic_dir / "_category_narratives.json"
    timeline_path = topic_dir / "_timeline_narrative.json"
    papers_path = Path(PAPERS_DIR) / "_papers_index.json"
    papers = json.loads(papers_path.read_text(encoding="utf-8"))
    topic_papers = [paper for paper in papers if topic in paper.get("topics", [])]
    category_papers = defaultdict(list)
    for paper in topic_papers:
        classification = paper.get("classifications", {}).get(topic, {})
        paper = dict(paper)
        paper["primary_category"] = classification.get("primary_category", "")
        paper["sub_category"] = classification.get("sub_category", "")
        paper["year"] = str(paper.get("date", ""))[:4]
        if paper["primary_category"]:
            category_papers[paper["primary_category"]].append(paper)

    existing_bytes = category_path.read_bytes() if category_path.exists() else None
    timeline_bytes = timeline_path.read_bytes() if timeline_path.exists() else None
    existing = json.loads(existing_bytes) if existing_bytes else []
    existing_by_category = {
        entry.get("category"): entry
        for entry in existing
        if isinstance(entry, dict) and isinstance(entry.get("category"), str)
    }
    selected = set(categories) if categories else None
    if generator is None:
        generator = TimelineCodex.production(topic_dir, policy)

    cached_categories: list[JsonObject] = []
    clean_categories: list[JsonObject] = []
    generated = 0
    reused = 0
    try:
        names = sorted(name for name in category_papers if name != "Other")
        for category in names:
            category_list = category_papers[category]
            source = _category_source(topic, category, category_list)
            input_hash = _sha256(source)
            previous = existing_by_category.get(category)
            force = bool(force_narrative or (selected is not None and category in selected))
            if not force and _entry_is_current(previous, input_hash):
                cached = cast(JsonObject, cast(object, previous))
                clean = cast(JsonObject, {
                    key: value
                    for key, value in cached.items()
                    if not key.startswith("_") and key != "source_sha256"
                })
                cached_categories.append(cached)
                clean_categories.append(clean)
                reused += 1
                continue
            task_hash = hashlib.sha256(category.encode("utf-8")).hexdigest()[:16]
            clean = generator.generate(
                prompt=_category_prompt(source),
                schema=CATEGORY_SCHEMA,
                schema_version="timeline-category-v1",
                prompt_version="timeline-category-prompt-v1",
                source=source,
                task_id=f"timeline-category:{topic}:{task_hash}",
                validator=lambda value, c=category, n=len(category_list): _validate_category(value, c, n),
            )
            cached = cast(JsonObject, dict(clean))
            cached["source_sha256"] = input_hash
            cached_categories.append(cached)
            clean_categories.append(clean)
            generated += 1

        executive_source = cast(JsonObject, cast(object, {
            "categories": clean_categories,
            "topic": topic,
        }))
        executive = generator.generate(
            prompt=_executive_prompt(topic, clean_categories),
            schema=EXECUTIVE_SCHEMA,
            schema_version="timeline-executive-v1",
            prompt_version="timeline-executive-prompt-v1",
            source=executive_source,
            task_id=f"timeline-executive:{topic}",
            validator=_validate_executive,
        )
        executive_text = executive.get("executive_summary_ko")
        if not isinstance(executive_text, str):
            raise TimelineGenerationError("executive result missing")
        timeline = _timeline_artifact(executive_text, clean_categories)
    except (GenerationCacheError, TimelineGenerationError) as error:
        return {
            "status": "failed",
            "reason": type(error).__name__,
            "category_bytes_preserved": existing_bytes is not None,
            "timeline_bytes_preserved": timeline_bytes is not None,
        }

    topic_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(category_path, cached_categories)
    atomic_write_json(timeline_path, timeline)
    return {
        "status": "ok",
        "generated": generated,
        "reused": reused,
        "category_count": len(clean_categories),
        "images": "skipped",
        "artifacts": [str(category_path), str(timeline_path)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate strict text-only timelines")
    _ = parser.add_argument("--topic", default="ai4s")
    _ = parser.add_argument("--candidates", type=int, default=3)
    _ = parser.add_argument("--narrative-only", action="store_true")
    _ = parser.add_argument("--images-only", action="store_true")
    _ = parser.add_argument("--main-only", action="store_true")
    _ = parser.add_argument("--category-only", action="store_true")
    _ = parser.add_argument("--categories", nargs="+")
    _ = parser.add_argument("--force-narrative", action="store_true")
    _ = parser.add_argument("--images", choices=["skip", "generate"], default="skip")
    _ = parser.add_argument("--llm-mode", default=None)
    args = parser.parse_args()
    try:
        policy = resolve_runtime_policy(
            cast(JsonObject, cast(object, load_config())),
            args.llm_mode,
        )
        result = _run_timeline(
            topic=args.topic,
            candidates=args.candidates,
            narrative_only=args.narrative_only,
            images_only=args.images_only,
            main_only=args.main_only,
            category_only=args.category_only,
            categories=args.categories,
            force_narrative=args.force_narrative,
            images=args.images,
            runtime_policy=policy,
        )
    except RuntimePolicyError as error:
        print(f"Runtime policy denied: {error.code}", file=sys.stderr)
        return 2
    if result.get("status") != "ok":
        print(f"Timeline stage failed: {result.get('reason', 'unknown')}")
        return 2
    print(
        f"Text timeline: categories={result['category_count']} "
        f"generated={result['generated']} reused={result['reused']} images=skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
