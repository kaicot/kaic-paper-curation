"""
LLM verification pass over generated paper-connections and cross-category insights.

Generated connections (``{slug: [{"slug": target, "relation", "reason"}]}``) and
cross-category insights (claims citing paper numbers) currently ship with **zero**
correctness checks. This module adds an opt-in, *best-effort* LLM audit that asks a
cheap Haiku-class model whether a claimed relation / cited evidence actually holds.

Design goals
------------
* **Dependency-light.** This module imports ONLY the Python standard library. The
  LLM client is *passed in* (any object exposing the Anthropic ``messages.create``
  shape, or a stub with the same interface), so importing ``verify`` never pulls in
  ``umap`` / ``hdbscan`` / ``sentence-transformers`` / SPECTER2. That keeps it
  importable in lightweight contexts (tests, CI) and trivially mockable.
* **Never breaks the default path.** On ANY failure (no client, network error,
  malformed/empty JSON, missing essence) verification is *skipped* and the
  originals are kept untouched — connections stay ``kept=True`` and insights stay
  ``supported=True``. Nothing is ever dropped because of an error.

Public API
----------
``verify_connections(connections, essence_by_slug, client, sample_n)``
    Sample up to ``sample_n`` generated A→B edges and ask the model whether the
    claimed relation between the two papers is supported by their essences.
    Returns a list of per-connection verdict records
    ``{source, target, relation, reason, kept, verdict, verify_reason}``.

``verify_insights(insights, paper_meta, client)``
    For each cross-category insight, ask the model whether its cited papers
    actually back the claim. Annotates each insight *in place* with
    ``supported: bool`` (+ ``verify_reason``) and returns the list of insights.

Integration helpers (thin env-driven wrappers used by the pipeline hooks)
``apply_connection_verification(connections, essence_by_slug, client, ...)``
``apply_insight_verification(insights, paper_meta, client, ...)``
"""

import json
import os
import random
import re

# Cheap auditor model. Overridable via env for cost/quality tuning.
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "claude-haiku-4-5")

# Human-readable relation definitions mirroring topic_modeling's connection types.
_RELATION_DEFS = {
    "alternative": "same problem, different approach",
    "extension": "builds on or extends the other work",
    "foundation": "provides a theoretical/methodological foundation",
    "counterpoint": "opposite perspective or critique",
    "application": "applies the other paper's method to a real problem",
}


# ─────────────────────────────────────────────────────────────────────────────
# Small pure helpers (no I/O, no deps)
# ─────────────────────────────────────────────────────────────────────────────

def _slug_num(slug):
    """'045_Foo_Bar' -> '045'. Pass through if there is no underscore."""
    return str(slug).split("_")[0]


def _canon_num(slug):
    """Canonical comparable id: drop leading zeros from a numeric prefix.

    '045'->'45', '45'->'45', '045_Foo'->'45'; non-numeric tokens pass through.
    Lets a model that emits an unpadded number ('45') still match a corpus keyed
    by the zero-padded NNN form ('045'), avoiding false 'not found' → false drops.
    """
    tok = _slug_num(slug)
    return (tok.lstrip("0") or "0") if tok.isdigit() else tok


def _short(text, n):
    """Collapse whitespace and truncate to ``n`` chars (None-safe)."""
    return (text or "").strip().replace("\n", " ")[:n]


def _essence_of(essence_by_slug, slug):
    """Resolve an essence/abstract for ``slug`` from a flexible mapping.

    Accepts ``{slug: text}`` or ``{slug: {"essence"|"abstract"|"title": ...}}`` and
    falls back to a bare-number key ('045') when the full slug is not present.
    """
    v = essence_by_slug.get(slug)
    if v is None:
        v = essence_by_slug.get(_slug_num(slug))
    if v is None:
        cnum = _canon_num(slug)
        for k, val in essence_by_slug.items():
            if _canon_num(k) == cnum:
                v = val
                break
    if isinstance(v, dict):
        return v.get("essence") or v.get("abstract") or v.get("title") or ""
    return v or ""


def _parse_json_block(text):
    """Best-effort extraction of the first JSON object/array from model text."""
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    raise ValueError("no JSON found in model response")


def _call_text(client, prompt, *, max_tokens=300, model=None):
    """One Haiku-class call; return the first content block's text.

    Works with the real Anthropic SDK client and with stubs exposing the same
    ``messages.create(...)`` → ``resp.content[0].text`` shape.
    """
    resp = client.messages.create(
        model=model or VERIFY_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    block = resp.content[0]
    return (getattr(block, "text", None) or "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Core: connection verification
# ─────────────────────────────────────────────────────────────────────────────

def _verify_one_connection(client, src, tgt, relation, reason, ess_a, ess_b):
    rel_def = _RELATION_DEFS.get(relation, relation or "related")
    prompt = (
        "You are auditing an automatically-generated 'related papers' link.\n\n"
        f"Paper A [{_slug_num(src)}]: {_short(ess_a, 600)}\n\n"
        f"Paper B [{_slug_num(tgt)}]: {_short(ess_b, 600)}\n\n"
        f'Claimed relation: A relates to B as "{relation}" ({rel_def}).\n'
        f"Claimed reason: {_short(reason, 300)}\n\n"
        "Judging ONLY from the two descriptions above, is this a genuine, "
        "topically meaningful connection (the two papers really share the claimed "
        "relationship or research area)? A link between papers on completely "
        "unrelated topics is NOT supported, even if a few generic words overlap.\n"
        "Respond with ONLY a JSON object, no prose:\n"
        '{"kept": true|false, "verdict": "supported"|"spurious", '
        '"reason": "<=1 short sentence"}'
    )
    data = _parse_json_block(_call_text(client, prompt, max_tokens=300))
    kept = bool(data.get("kept", True))
    verdict = str(data.get("verdict") or ("supported" if kept else "spurious"))
    return {"kept": kept, "verdict": verdict,
            "verify_reason": str(data.get("reason", ""))[:200]}


def verify_connections(connections, essence_by_slug, client, sample_n=12):
    """Audit generated paper-connections for spurious links.

    Args:
        connections: ``{source_slug: [{"slug"|"target", "relation", "reason"}, ...]}``
        essence_by_slug: ``{slug: text}`` or ``{slug: {"essence"/"title": ...}}``.
        client: LLM client with ``.messages.create`` (Anthropic or stub). ``None``
            means verification is impossible → returns ``[]`` (caller keeps all).
        sample_n: max number of edges to verify; ``<= 0`` verifies all edges.

    Returns:
        ``list`` of per-connection records, each
        ``{source, target, relation, reason, kept, verdict, verify_reason}``.
        A per-edge API/parse error defaults that edge to ``kept=True``,
        ``verdict="error"`` so nothing is dropped on failure.
    """
    if client is None or not connections:
        return []

    edges = []
    for src, conns in connections.items():
        for c in (conns or []):
            tgt = c.get("slug") or c.get("target")
            if not tgt:
                continue
            edges.append((src, tgt, c.get("relation", ""), c.get("reason", "")))
    if not edges:
        return []

    if sample_n and sample_n > 0 and len(edges) > sample_n:
        # Deterministic sample so repeated runs audit the same edges.
        edges_sample = random.Random(1234).sample(edges, sample_n)
    else:
        edges_sample = edges

    results = []
    for src, tgt, relation, reason in edges_sample:
        rec = {"source": src, "target": tgt, "relation": relation,
               "reason": reason, "kept": True, "verdict": "error",
               "verify_reason": ""}
        try:
            v = _verify_one_connection(
                client, src, tgt, relation, reason,
                _essence_of(essence_by_slug, src),
                _essence_of(essence_by_slug, tgt))
            rec.update(v)
        except Exception as e:  # graceful: keep edge on any failure
            rec["kept"] = True
            rec["verdict"] = "error"
            rec["verify_reason"] = f"verify-error: {str(e)[:80]}"
        results.append(rec)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Core: insight verification
# ─────────────────────────────────────────────────────────────────────────────

def _cited_meta(ins, paper_meta):
    """Render the papers an insight cites (by number) as readable evidence lines."""
    nums = ins.get("evidence") or ins.get("paper_numbers") or ins.get("papers") or []
    if isinstance(nums, str):
        nums = [nums]
    lines = []
    for n in list(nums)[:12]:
        key = str(n)
        v = paper_meta.get(key)
        if v is None:
            v = paper_meta.get(_slug_num(key))
        if v is None and key.isdigit():
            v = paper_meta.get(key.zfill(3))
        if v is None:
            ckey = _canon_num(key)
            for k, val in paper_meta.items():
                if _canon_num(k) == ckey:
                    v = val
                    break
        if isinstance(v, dict):
            txt = v.get("essence") or v.get("abstract") or v.get("title") or ""
            lines.append(f"[{key}] {v.get('title', '')} | {_short(txt, 300)}")
        elif v:
            lines.append(f"[{key}] {_short(v, 300)}")
        else:
            lines.append(f"[{key}] (not found in corpus)")
    return lines


def _verify_one_insight(client, ins, cited_lines):
    cited_block = "\n".join(cited_lines) if cited_lines else "(no papers cited)"
    prompt = (
        "You are auditing an automatically-generated cross-category research "
        "insight.\n\n"
        f"Insight type: {ins.get('type', '')}\n"
        f"Title: {ins.get('title', '')}\n"
        f"Claim: {_short(ins.get('description', ''), 600)}\n\n"
        "Papers cited as evidence for this claim:\n"
        f"{cited_block}\n\n"
        "Do the cited papers actually support this claim? It is supported ONLY if "
        "at least some cited papers are clearly relevant to the claim. If the cited "
        "papers are about unrelated topics, or no real papers are cited, the claim "
        "is NOT supported.\n"
        "Respond with ONLY a JSON object, no prose:\n"
        '{"supported": true|false, "reason": "<=1 short sentence"}'
    )
    data = _parse_json_block(_call_text(client, prompt, max_tokens=300))
    return {"supported": bool(data.get("supported", True)),
            "reason": str(data.get("reason", ""))}


def verify_insights(insights, paper_meta, client):
    """Annotate each cross-category insight with ``supported: bool``.

    Args:
        insights: the full insights dict (with ``"cross_category"``) OR a plain
            list of insight dicts. Each insight has at least
            ``{"title", "description", "evidence"}`` (evidence = cited paper nums).
        paper_meta: ``{num_or_slug: text}`` or ``{num_or_slug: {"title"/"essence"}}``.
        client: LLM client (Anthropic/stub). ``None`` → annotate ``supported=True``.

    Returns:
        the list of insight dicts (same objects, annotated *in place*). On any
        per-insight error that insight defaults to ``supported=True`` (never
        dropped here — dropping is the caller's policy).
    """
    items = insights.get("cross_category", []) if isinstance(insights, dict) \
        else (insights or [])
    if not items:
        return items
    for ins in items:
        ins.setdefault("supported", True)
        ins.setdefault("verify_reason", "")
        if client is None:
            ins["verify_reason"] = "skipped (no client)"
            continue
        try:
            v = _verify_one_insight(client, ins, _cited_meta(ins, paper_meta))
            ins["supported"] = bool(v.get("supported", True))
            ins["verify_reason"] = str(v.get("reason", ""))[:200]
        except Exception as e:  # graceful: keep insight on any failure
            ins["supported"] = True
            ins["verify_reason"] = f"verify-error: {str(e)[:80]}"
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline hooks (env-driven wrappers; never raise)
# ─────────────────────────────────────────────────────────────────────────────

def _conn_mode():
    raw = (os.environ.get("VERIFY_CONNECTIONS", "sample") or "sample").strip().lower()
    return raw if raw in ("off", "sample", "strict") else "sample"


def _insights_enabled():
    raw = (os.environ.get("VERIFY_INSIGHTS", "1") or "1").strip().lower()
    return raw not in ("0", "off", "false", "no")


def apply_connection_verification(connections, essence_by_slug, client,
                                  *, mode=None, sample_n=None, log=print):
    """topic_modeling / extract_insights hook for paper-connections.

    mode (default from env ``VERIFY_CONNECTIONS``, fallback ``sample``):
      ``off``    no-op.
      ``sample`` verify a sample, LOG spurious-looking edges, keep ALL.
      ``strict`` verify a sample, DROP spurious edges from ``connections`` in place.

    ``sample_n`` defaults to env ``VERIFY_CONNECTIONS_SAMPLE`` (12). Returns the
    (possibly pruned) ``connections`` dict. Never raises.
    """
    try:
        mode = mode or _conn_mode()
        if mode == "off":
            return connections
        if not connections or client is None:
            log("  [verify] connections: skipped (no client / no connections)")
            return connections
        if sample_n is None:
            sample_n = int(os.environ.get("VERIFY_CONNECTIONS_SAMPLE", "12"))
        results = verify_connections(connections, essence_by_slug, client,
                                     sample_n=sample_n)
        spurious = [r for r in results
                    if not r.get("kept", True) and r.get("verdict") != "error"]
        log(f"  [verify] connections: checked {len(results)}, "
            f"{len(spurious)} flagged spurious (mode={mode})")
        for r in spurious:
            log(f"    [verify] spurious {_slug_num(r['source'])}->"
                f"{_slug_num(r['target'])} ({r.get('relation')}): "
                f"{r.get('verify_reason', '')[:80]}")
        if mode == "strict" and spurious:
            drop = {(r["source"], r["target"]) for r in spurious}
            removed = 0
            for src, conns in list(connections.items()):
                kept = [c for c in (conns or [])
                        if (src, c.get("slug") or c.get("target")) not in drop]
                removed += len(conns or []) - len(kept)
                connections[src] = kept
            log(f"  [verify] connections: dropped {removed} spurious edge(s) (strict)")
        return connections
    except Exception as e:
        log(f"  [verify] connections: skipped on error ({str(e)[:80]})")
        return connections


def apply_insight_verification(insights, paper_meta, client, *, drop=True, log=print):
    """extract_insights hook for cross-category insights.

    Controlled by env ``VERIFY_INSIGHTS`` (default on; ``0``/``off``/``false`` to
    disable). Annotates each ``cross_category`` insight with ``supported: bool``.
    When ``drop`` is True (default) unsupported insights are removed from
    ``insights['cross_category']`` and stashed under ``insights['_verify_dropped']``
    for traceability. Returns the insights dict. Never raises.
    """
    try:
        if not _insights_enabled():
            log("  [verify] insights: disabled (VERIFY_INSIGHTS=0)")
            return insights
        items = insights.get("cross_category", []) if isinstance(insights, dict) \
            else insights
        if not items or client is None:
            log("  [verify] insights: skipped (no client / no insights)")
            return insights
        verify_insights(insights, paper_meta, client)
        unsupported = [i for i in items if not i.get("supported", True)]
        log(f"  [verify] insights: checked {len(items)}, "
            f"{len(unsupported)} unsupported")
        for i in unsupported:
            log(f"    [verify] unsupported insight '{i.get('title', '')}': "
                f"{i.get('verify_reason', '')[:80]}")
        if drop and unsupported and isinstance(insights, dict):
            insights["cross_category"] = [i for i in items
                                          if i.get("supported", True)]
            insights.setdefault("_verify_dropped", []).extend(unsupported)
            log(f"  [verify] insights: dropped {len(unsupported)} unsupported")
        return insights
    except Exception as e:
        log(f"  [verify] insights: skipped on error ({str(e)[:80]})")
        return insights
