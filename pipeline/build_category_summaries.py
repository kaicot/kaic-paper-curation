"""Generate transactional Korean category and sub-theme summary artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast, final

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
SUMMARY_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "category-summary-v1.json"
with SUMMARY_SCHEMA_PATH.open("r", encoding="utf-8") as _schema_handle:
    SUMMARY_SCHEMA = cast(JsonObject, json.load(_schema_handle))


class SummaryGenerationError(RuntimeError):
    """A bounded category-summary generation exhausted its valid attempts."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def validate_description(text: str, label: str) -> str | None:
    if len(text) < 50:
        return f"{label}: too short ({len(text)} chars)"
    korean = len(re.findall(r"[가-힣]", text))
    if korean < len(text) * 0.3:
        return f"{label}: Korean ratio too low"
    if "[NNN]" in text:
        return f"{label}: literal [NNN] remained"
    if text[-1] not in ".!?。다요음임함됨됨.":
        return f"{label}: missing terminal punctuation"
    return None


@final
class SummaryCodex:
    """Bounded short-form generation with success-only cache publication."""

    def __init__(self, runtime_policy, gateway, cache, identity_factory=CacheIdentity.from_gateway):
        self.runtime_policy = runtime_policy
        self.gateway = gateway
        self.cache = cache
        self.identity_factory = identity_factory

    @classmethod
    def production(cls, topic_dir: str | Path, runtime_policy: RuntimePolicy) -> "SummaryCodex":
        if runtime_policy.mode != "codex":
            raise GenerationCacheError(
                "policy-denied",
                "category summary generation requires codex mode",
            )
        return cls(
            runtime_policy,
            CodexGateway.production(ROOT),
            GenerationCache(Path(topic_dir) / ".llm_cache"),
        )

    def generate_korean(
        self,
        *,
        prompt: str,
        source: JsonObject,
        task_id: str,
        label: str,
        max_attempts: int = 2,
    ) -> str:
        issue_hint = ""
        for attempt in range(1, max_attempts + 1):
            attempt_prompt = prompt + issue_hint
            last_issue = "schema-invalid"
            identity = self.identity_factory(
                runtime_policy=self.runtime_policy,
                gateway=self.gateway,
                role="short_form",
                prompt_version="category-summary-prompt-v1",
                prompt=attempt_prompt,
                schema_version="category-summary-v1",
                schema=SUMMARY_SCHEMA,
                source=_canonical(source),
                task_id=task_id,
            )

            def produce():
                nonlocal last_issue
                try:
                    value = self.gateway.generate_json(
                        "short_form",
                        attempt_prompt,
                        SUMMARY_SCHEMA,
                    )
                    validate_json(value, SUMMARY_SCHEMA)
                    if set(value) != {"description_ko"}:
                        raise SummaryGenerationError("summary fields must be exact")
                    text = value.get("description_ko")
                    if not isinstance(text, str):
                        raise SummaryGenerationError("description_ko must be text")
                    issue = validate_description(text.strip(), label)
                    if issue:
                        last_issue = issue
                        raise SummaryGenerationError(issue)
                    return CacheSuccess({"description_ko": text.strip()})
                except (
                    CodexGatewayError,
                    SchemaError,
                    SummaryGenerationError,
                    TypeError,
                    ValueError,
                ):
                    return CacheFailure("failed")

            try:
                result = self.cache.get_or_generate(identity, produce)
            except GenerationCacheError:
                if attempt == max_attempts:
                    raise SummaryGenerationError(last_issue)
                issue_hint = (
                    "\n\nPrevious output failed this deterministic quality check: "
                    f"{last_issue}. Return a corrected Korean description."
                )
                continue
            text = result.get("description_ko")
            if not isinstance(text, str):
                raise SummaryGenerationError("cached description is invalid")
            issue = validate_description(text, label)
            if issue:
                raise SummaryGenerationError(issue)
            return text
        raise SummaryGenerationError("bounded attempts exhausted")


def collect_sub_themes_from_index(cat_name, papers, topic):
    """Collect deterministic sub-theme names from classification artifacts."""
    sub_counts = Counter()
    for paper in papers:
        classification = paper.get("classifications", {}).get(topic, {})
        sub_category = classification.get("sub_categories", {}).get(cat_name, "")
        if not sub_category and classification.get("primary_category") == cat_name:
            sub_category = classification.get("sub_category", "")
        if sub_category:
            sub_counts[sub_category] += 1
    return [
        {
            "name": name,
            "description": f"{name} ({count} papers in {cat_name})",
            "count": count,
        }
        for name, count in sub_counts.most_common()
    ]


def _paper_source(paper, topic):
    return {
        "classifications": paper.get("classifications", {}).get(topic, {}),
        "date": paper.get("date", ""),
        "essence": paper.get("essence", ""),
        "score": paper.get("score", 0),
        "slug": paper.get("slug", ""),
        "title": paper.get("title", ""),
    }


def _category_source(category, papers, sub_themes, topic):
    return {
        "category": category,
        "papers": [
            _paper_source(paper, topic)
            for paper in sorted(papers, key=lambda item: item.get("slug", ""))
        ],
        "sub_themes": sub_themes,
        "topic": topic,
    }


def _entry_is_current(entry, source_sha256):
    if entry.get("source_sha256") != source_sha256:
        return False
    category = str(entry.get("category", ""))
    description = entry.get("description_ko", "")
    if not isinstance(description, str) or validate_description(description, category):
        return False
    for sub_theme in entry.get("sub_themes", []):
        if not isinstance(sub_theme, dict):
            return False
        label = f"{category}/{sub_theme.get('name', '')}"
        text = sub_theme.get("description_ko", "")
        if not isinstance(text, str) or validate_description(text, label):
            return False
    return True


def _overview_prompt(topic, category, papers, sub_themes):
    top = sorted(papers, key=lambda item: -item.get("score", 0))[:20]
    paper_block = "\n".join(
        f"[{str(paper.get('slug', '')).split('_')[0]}] {str(paper.get('title', ''))[:60]}"
        for paper in top
    )
    themes = ", ".join(str(theme.get("name", "")) for theme in sub_themes)
    return f"""{topic} category "{category}" ({len(papers)} papers) overview.
Representative papers:
{paper_block}
Sub-themes: {themes or 'none'}

Return a Korean 4-6 sentence description matching the supplied schema.
Keep technical terms in English, cite papers with the listed numeric [N] markers,
do not repeat paper titles, and end with a complete sentence."""


def _subtheme_prompt(category, sub_theme, papers):
    top = sorted(papers, key=lambda item: -item.get("score", 0))[:8]
    paper_block = "\n".join(
        f"[{str(paper.get('slug', '')).split('_')[0]}] {str(paper.get('title', ''))[:60]}"
        for paper in top
    )
    return f"""Category "{category}" sub-theme "{sub_theme['name']}" overview.
{paper_block}

Return a Korean 4-6 sentence description matching the supplied schema.
Keep technical terms in English, include at least two listed numeric citations,
and end with a complete sentence."""


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_category_summary(
    topic="ai4s",
    *,
    regen_ko=False,
    categories=None,
    runtime_policy=None,
    generator=None,
):
    """Build a complete candidate artifact and publish only after all gates pass."""
    policy = runtime_policy or resolve_runtime_policy(
        cast(JsonObject, cast(object, load_config()))
    )
    if policy.mode != "codex" and generator is None:
        return {"status": "unavailable", "reason": "runtime-off"}
    topic_dir = Path(get_topic_dir(topic))
    summary_path = topic_dir / "_category_summaries.json"
    previous_path = topic_dir / "_category_summaries.previous.json"
    failure_path = topic_dir / "_category_summaries.failed.json"

    papers_path = Path(PAPERS_DIR) / "_papers_index.json"
    papers = json.loads(papers_path.read_text(encoding="utf-8"))
    topic_papers = [paper for paper in papers if topic in paper.get("topics", [])]
    category_papers = defaultdict(list)
    sub_papers = defaultdict(list)
    for paper in topic_papers:
        classification = paper.get("classifications", {}).get(topic, {})
        category = classification.get("primary_category", "Other")
        category_papers[category].append(paper)
        sub_category = classification.get("sub_categories", {}).get(
            category,
            classification.get("sub_category", ""),
        )
        sub_papers[(category, sub_category)].append(paper)

    existing_bytes = summary_path.read_bytes() if summary_path.exists() else None
    existing = json.loads(existing_bytes) if existing_bytes else []
    existing_by_category = {
        entry.get("category"): entry
        for entry in existing
        if isinstance(entry, dict) and isinstance(entry.get("category"), str)
    }
    selected = set(categories) if categories else None
    if generator is None:
        generator = SummaryCodex.production(topic_dir, policy)

    candidates = []
    generated = 0
    reused = 0
    try:
        category_names = sorted(
            (name for name in category_papers if name != "Other"),
        ) + (["Other"] if "Other" in category_papers else [])
        for category in category_names:
            category_list = category_papers[category]
            sub_themes = (
                collect_sub_themes_from_index(category, category_list, topic)
                if len(category_list) > 30
                else []
            )
            source = cast(
                JsonObject,
                cast(
                    object,
                    _category_source(category, category_list, sub_themes, topic),
                ),
            )
            source_sha256 = _sha256(source)
            existing_entry = existing_by_category.get(category)
            force = bool(
                (selected is not None and category in selected)
                or (regen_ko and selected is None)
            )
            if (
                not force
                and isinstance(existing_entry, dict)
                and _entry_is_current(existing_entry, source_sha256)
            ):
                candidates.append(existing_entry)
                reused += 1
                continue

            top = sorted(category_list, key=lambda item: -item.get("score", 0))[:20]
            entry = cast(JsonObject, cast(object, {
                "avg_score": round(
                    sum(paper.get("score", 0) for paper in category_list)
                    / max(1, len(category_list)),
                    2,
                ),
                "category": category,
                "count": len(category_list),
                "description": f"AI for Science category: {category}",
                "papers": [
                    {
                        "date": paper.get("date", ""),
                        "dir": paper["slug"],
                        "score": paper.get("score", 0),
                        "slug": paper["slug"],
                        "title": paper["title"],
                    }
                    for paper in top
                ],
                "source_sha256": source_sha256,
                "sub_themes": sub_themes,
            }))
            task_hash = hashlib.sha256(category.encode("utf-8")).hexdigest()[:16]
            entry["description_ko"] = generator.generate_korean(
                prompt=_overview_prompt(topic, category, category_list, sub_themes),
                source=source,
                task_id=f"category-summary:{topic}:{task_hash}",
                label=category,
            )
            sub_theme_rows = cast(
                list[JsonObject],
                cast(object, entry["sub_themes"]),
            )
            for sub_theme in sub_theme_rows:
                name = sub_theme["name"]
                sub_source = cast(JsonObject, cast(object, {
                    "category_source_sha256": source_sha256,
                    "papers": [
                        _paper_source(paper, topic)
                        for paper in sub_papers.get((category, name), [])
                    ],
                    "sub_theme": sub_theme,
                }))
                sub_hash = hashlib.sha256(
                    f"{category}\0{name}".encode("utf-8")
                ).hexdigest()[:16]
                sub_theme["description_ko"] = generator.generate_korean(
                    prompt=_subtheme_prompt(
                        category,
                        sub_theme,
                        sub_papers.get((category, name), []),
                    ),
                    source=sub_source,
                    task_id=f"subtheme-summary:{topic}:{sub_hash}",
                    label=f"{category}/{name}",
                )
            candidates.append(entry)
            generated += 1
    except (GenerationCacheError, SummaryGenerationError) as error:
        topic_dir.mkdir(parents=True, exist_ok=True)
        if existing_bytes is not None:
            _atomic_write_bytes(previous_path, existing_bytes)
        atomic_write_json(
            failure_path,
            {
                "error": type(error).__name__,
                "result": "failed",
                "schema_version": 1,
                "topic": topic,
            },
        )
        return {
            "status": "failed",
            "reason": "summary-generation-failed",
            "generated": generated,
            "reused": reused,
        }

    topic_dir.mkdir(parents=True, exist_ok=True)
    if existing_bytes is not None:
        _atomic_write_bytes(previous_path, existing_bytes)
    atomic_write_json(summary_path, candidates)
    failure_path.unlink(missing_ok=True)
    return {
        "status": "ok",
        "generated": generated,
        "reused": reused,
        "summaries": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build _category_summaries.json")
    _ = parser.add_argument("--topic", default="ai4s")
    _ = parser.add_argument("--regen-ko", action="store_true")
    _ = parser.add_argument("--categories", nargs="+", default=None)
    _ = parser.add_argument("--llm-mode", default=None)
    args = parser.parse_args()
    categories = (
        [value.strip() for value in args.categories if value.strip()]
        if args.categories
        else None
    )
    try:
        policy = resolve_runtime_policy(
            cast(JsonObject, cast(object, load_config())),
            args.llm_mode,
        )
        result = _run_category_summary(
            topic=args.topic,
            regen_ko=args.regen_ko,
            categories=categories,
            runtime_policy=policy,
        )
    except RuntimePolicyError as error:
        print(f"Runtime policy denied: {error.code}", file=sys.stderr)
        return 2
    if result.get("status") != "ok":
        print(f"Category summary stage failed: {result.get('reason', 'unknown')}")
        return 2
    print(
        f"Saved category summaries: generated={result['generated']} reused={result['reused']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
