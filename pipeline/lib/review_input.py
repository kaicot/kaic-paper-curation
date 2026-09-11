"""Evidence-only, section-aware source preparation for paper reviews.

The review model must receive a bounded amount of PDF text, but a leading
slice of an article routinely omits the experimental evidence.  This module
selects literal excerpts from the sections that carry that evidence and records
exactly what was unavailable or shortened.  It never summarizes or rewrites
source text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final


REVIEW_INPUT_SCHEMA: Final = "review-input-v1"
DEFAULT_REVIEW_INPUT_LIMIT: Final = 40_000
CORE_SECTIONS: Final = ("methods", "results", "discussion", "conclusion")
_SELECTION_ORDER: Final = ("abstract", *CORE_SECTIONS)
_SECTION_WEIGHT: Final = {
    "abstract": 1,
    "methods": 3,
    "results": 4,
    "discussion": 3,
    "conclusion": 2,
}
_HOST_OMISSION: Final = "\n[PIPELINE: source excerpt omitted here]\n"


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str
    labels: tuple[str, ...]
    start: int
    end: int
    content_start: int
    content_end: int


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def model_fingerprint_from_identity(identity: Mapping[str, Any]) -> str:
    """Fingerprint the model semantics that actually produced one artifact.

    This intentionally uses a persisted generation identity rather than the
    current role configuration.  A later settings edit must not rewrite the
    historical provenance of a cached or already published review.
    """
    semantic = {
        name: identity.get(name)
        for name in ("role", "model", "reasoning_effort")
    }
    if not all(isinstance(value, str) and value for value in semantic.values()):
        raise ValueError("generation identity lacks model semantics")
    return hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalise_heading(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[\[\]():;,.·•*_`]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _heading_title(line: str) -> str | None:
    """Return a probable heading title without treating regular prose as one."""
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return None
    markdown = re.fullmatch(r"#{1,6}\s+(.+?)(?:\s+#+)?", stripped)
    if markdown:
        return markdown.group(1).strip()
    numbered = re.fullmatch(
        r"(?:(?:\d+(?:\.\d+){0,4}|[IVXLC]+)[.)]?)\s+(.+?)", stripped,
        flags=re.IGNORECASE,
    )
    if numbered:
        return numbered.group(1).strip()
    normal = _normalise_heading(stripped)
    compact_korean = re.fullmatch(
        r"(?:초록|요약|서론|연구 ?방법|방법|(?:연구 ?)?대상 및 방법|방법 및 (?:연구 ?)?대상|재료 및 방법|"
        r"결과(?: 및 (?:고찰|논의))?|고찰|논의|결론)",
        normal,
    )
    if compact_korean:
        return stripped
    if re.fullmatch(
        r"(?:abstract|introduction|materials? and methods?|(?:subjects?|patients?|participants?) and methods?|"
        r"methodology|experimental procedures?|results?(?: and discussion)?|discussion|conclusions?)",
        normal,
    ):
        return stripped
    return None


def _labels_for_heading(title: str) -> tuple[str, ...]:
    value = _normalise_heading(title)
    labels: list[str] = []
    if re.search(r"\babstract\b|초록|요약", value):
        labels.append("abstract")
    if re.search(
        r"\b(?:materials? and methods?|(?:subjects?|patients?|participants?) and methods?|methods?|"
        r"methodology|experimental procedures?)\b|"
        r"연구 ?방법|(?:연구 ?)?대상 및 방법|방법 및 (?:연구 ?)?대상|재료 및 방법|방법", value,
    ):
        labels.append("methods")
    if re.search(r"\bresults?\b|결과", value):
        labels.append("results")
    if re.search(r"\bdiscussion\b|고찰|논의", value):
        labels.append("discussion")
    if re.search(r"\bconclusions?\b|결론", value):
        labels.append("conclusion")
    return tuple(labels)


def _find_sections(text: str) -> list[_Section]:
    offsets: list[tuple[int, int, str, tuple[str, ...]]] = []
    position = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        title = _heading_title(line)
        labels = _labels_for_heading(title) if title is not None else ()
        normal = _normalise_heading(title) if title is not None else ""
        # Only a semantic major heading may end a selected section.  Page
        # headers and all-caps subheadings otherwise cut Methods/Results into
        # fragments and silently hide their later evidence.
        is_introduction = normal in {"introduction", "서론"}
        if title is not None and (labels or is_introduction):
            offsets.append((position, position + len(raw_line), title, labels))
        position += len(raw_line)
    sections: list[_Section] = []
    for index, (start, content_start, heading, labels) in enumerate(offsets):
        if not labels:
            continue
        end = offsets[index + 1][0] if index + 1 < len(offsets) else len(text)
        sections.append(_Section(heading, labels, start, end, content_start, end))
    return sections


def _literal_excerpt(value: str, budget: int) -> tuple[str, bool]:
    """Keep source verbatim; for long sections retain their opening and ending."""
    if len(value) <= budget:
        return value, False
    if budget <= len(_HOST_OMISSION) + 2:
        return value[:budget], True
    first = max(1, int((budget - len(_HOST_OMISSION)) * 0.62))
    last = budget - len(_HOST_OMISSION) - first
    return value[:first] + _HOST_OMISSION + value[-last:], True


def _allocate(sections: list[_Section], text: str, limit: int) -> list[int]:
    capacities = [len(text[section.start:section.end]) for section in sections]
    if not sections:
        return []
    # Ensure that every selected evidence section remains represented before
    # giving the larger Results/Methods blocks their proportional extra space.
    minimums = [min(capacity, 900) for capacity in capacities]
    total_minimum = sum(minimums)
    if total_minimum > limit:
        raw = [limit * minimum // total_minimum for minimum in minimums]
        for index in sorted(range(len(raw)), key=lambda item: minimums[item] - raw[item], reverse=True):
            if sum(raw) >= limit:
                break
            raw[index] += 1
        return raw
    allocation = minimums[:]
    remaining = limit - total_minimum
    weights = [sum(_SECTION_WEIGHT[label] for label in section.labels) for section in sections]
    while remaining > 0:
        active = [index for index, capacity in enumerate(capacities) if allocation[index] < capacity]
        if not active:
            break
        total_weight = sum(weights[index] for index in active)
        progressed = False
        for index in active:
            extra = max(1, remaining * weights[index] // total_weight)
            grant = min(extra, capacities[index] - allocation[index], remaining)
            allocation[index] += grant
            remaining -= grant
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    return allocation


def _fallback(text: str, limit: int) -> tuple[str, list[dict[str, int]], bool]:
    if len(text) <= limit:
        return text, [{"start": 0, "end": len(text)}], False
    # Host omission markers count toward the caller's hard bound.  For a tiny
    # budget one literal chunk is more useful than overflowing the limit.
    maximum_chunks = max(1, (limit + len(_HOST_OMISSION)) // (len(_HOST_OMISSION) + 1))
    preferred_chunks = min(4, max(2, limit // 1_000))
    chunk_count = min(preferred_chunks, maximum_chunks)
    payload_budget = limit - len(_HOST_OMISSION) * (chunk_count - 1)
    chunk_size = max(1, payload_budget // chunk_count)
    starts = (
        [0]
        if chunk_count == 1
        else [round((len(text) - chunk_size) * index / (chunk_count - 1)) for index in range(chunk_count)]
    )
    chunks = [text[start:start + chunk_size] for start in starts]
    excerpt = _HOST_OMISSION.join(chunks)
    locations = [{"start": start, "end": start + len(chunk)} for start, chunk in zip(starts, chunks)]
    return excerpt, locations, True


def build_review_source(text: str, *, max_characters: int = DEFAULT_REVIEW_INPUT_LIMIT) -> tuple[str, dict[str, Any]]:
    """Return bounded literal evidence text and a complete selection record.

    The returned text has deterministic host delimiters only.  Every other
    character originates in ``text``.  Invalid or too-small budgets are caller
    errors because a review cannot be evidence-bound without usable context.
    """
    if not isinstance(text, str):
        raise TypeError("review source text must be a string")
    if isinstance(max_characters, bool) or not isinstance(max_characters, int) or max_characters < 256:
        raise ValueError("max_characters must be an integer of at least 256")

    all_sections = _find_sections(text)
    detected_labels = [
        label for label in _SELECTION_ORDER
        if any(label in section.labels for section in all_sections)
    ]
    undetected = [label for label in CORE_SECTIONS if label not in detected_labels]
    selected: list[_Section] = []
    selected_ids: set[int] = set()
    for label in _SELECTION_ORDER:
        candidate = next((section for section in all_sections if label in section.labels), None)
        if candidate is not None and id(candidate) not in selected_ids:
            selected.append(candidate)
            selected_ids.add(id(candidate))

    source_hash = _sha256(text)
    if len(text) <= max_characters:
        # Heading extraction is only a navigation aid.  It must never turn a
        # complete, affordable article into a 4–5k heading fragment.
        metadata: dict[str, Any] = {
            "schema_version": REVIEW_INPUT_SCHEMA,
            "selection_mode": "full_document",
            "max_characters": max_characters,
            "source_characters": len(text),
            "source_sha256": source_hash,
            "selected_characters": len(text),
            "selected_sha256": source_hash,
            "selected_sections": detected_labels,
            "missing_sections": [],
            "undetected_section_headings": undetected,
            "truncated_sections": [],
            "sections": [
                {"heading": section.heading, "labels": list(section.labels),
                 "source_characters": section.end - section.start,
                 "selected_characters": section.end - section.start, "truncated": False}
                for section in all_sections
            ],
        }
        return text, metadata
    if not selected:
        excerpt, locations, truncated = _fallback(text, max_characters)
        metadata: dict[str, Any] = {
            "schema_version": REVIEW_INPUT_SCHEMA,
            "selection_mode": "distributed_fallback",
            "max_characters": max_characters,
            "source_characters": len(text),
            "source_sha256": source_hash,
            "selected_characters": len(excerpt),
            "selected_sha256": _sha256(excerpt),
            "selected_sections": [],
            "missing_sections": [],
            "undetected_section_headings": undetected,
            "truncated_sections": ["unstructured_source"] if truncated else [],
            "fallback_excerpts": locations,
        }
        return excerpt, metadata

    prefixes = ["[SECTION " + ",".join(section.labels) + "]\n" for section in selected]
    fallback_prefix = "\n[SECTION distributed-context]\n"
    # Preserve a distributed view of the unclassified remainder.  This avoids
    # dropping appendices, limitations, later result paragraphs, and PDF text
    # whose headings could not be recognized, while still prioritizing evidence
    # sections in the other three quarters of the bound.
    reserved_fallback_budget = max(1, max_characters // 4 - len(fallback_prefix))
    section_budget = max(0, max_characters - reserved_fallback_budget - len(fallback_prefix) - sum(len(prefix) for prefix in prefixes))
    source_budget = section_budget
    budgets = _allocate(selected, text, source_budget)
    rendered: list[str] = []
    truncated_sections: list[dict[str, Any]] = []
    selected_labels: list[str] = []
    section_records: list[dict[str, Any]] = []
    for section, prefix, budget in zip(selected, prefixes, budgets):
        full = text[section.start:section.end]
        excerpt, truncated = _literal_excerpt(full, budget)
        labels = list(section.labels)
        for label in labels:
            if label not in selected_labels:
                selected_labels.append(label)
        rendered.append(prefix + excerpt)
        record = {
            "heading": section.heading,
            "labels": labels,
            "source_characters": len(full),
            "selected_characters": len(excerpt),
            "truncated": truncated,
        }
        section_records.append(record)
        if truncated:
            truncated_sections.append(record)
    # If the selected sections are shorter than their reservation, give the
    # remainder to distributed source coverage rather than leaving budget idle.
    fallback_budget = max(1, max_characters - len("".join(rendered)) - len(fallback_prefix))
    fallback_source, fallback_locations, fallback_truncated = _fallback(text, fallback_budget)
    source = "".join(rendered) + fallback_prefix + fallback_source
    metadata = {
        "schema_version": REVIEW_INPUT_SCHEMA,
        "selection_mode": "section_aware",
        "max_characters": max_characters,
        "source_characters": len(text),
        "source_sha256": source_hash,
        "selected_characters": len(source),
        "selected_sha256": _sha256(source),
        "selected_sections": selected_labels,
        "missing_sections": [],
        "undetected_section_headings": undetected,
        "truncated_sections": truncated_sections,
        "sections": section_records,
        "fallback_excerpts": fallback_locations,
        "fallback_truncated": fallback_truncated,
    }
    return source, metadata


def review_generation_currentness(
    provenance: Mapping[str, Any],
    source_bytes: bytes,
    review_bytes: bytes,
    *,
    prompt_version: str,
    model_fingerprint: str,
    routing_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Report whether a review sidecar remains comparable to current inputs."""
    input_record = provenance.get("review_input")
    recorded_source = input_record.get("source_sha256") if isinstance(input_record, Mapping) else None
    recorded_prompt = provenance.get("prompt_version")
    recorded_model = provenance.get("model_fingerprint")
    current_source = _sha256(source_bytes)
    current_review = _sha256(review_bytes)
    reasons: list[str] = []
    if provenance.get("review_sha256") != current_review:
        reasons.append("review-artifact-changed")
    if recorded_source != current_source:
        reasons.append("source-changed")
    if recorded_prompt != prompt_version:
        reasons.append("prompt-changed")
    if recorded_model != model_fingerprint:
        reasons.append("model-routing-changed")
    if routing_fingerprint is not None and provenance.get("routing_fingerprint") != routing_fingerprint:
        reasons.append("configured-routing-changed")
    return {
        "current": not reasons,
        "reasons": reasons,
        "source_sha256": current_source,
        "review_sha256": current_review,
        "prompt_version": prompt_version,
        "model_fingerprint": model_fingerprint,
        "routing_fingerprint": routing_fingerprint,
    }
