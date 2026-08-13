"""
Paper-Curation --local --update-force 배치 실행 스크립트.

사용법:
  PYTHONUTF8=1 python run_update_force.py --topic ai4s
  # --concurrency 기본값 16.

기능:
  1. Zotero 컬렉션에서 전체 논문 fetch
  2. 기존 review.md, text.md, figures/ 삭제
  3. PDF 파싱 → text.md
  4. Figure 추출 + 결정론적 geometric 검증
  5. review.md 작성 (saved-auth Codex)
  6. index.html 변환 (review_to_html.py)
  7. 진행 상황을 checkpoint.json에 저장 (중단 후 재개 가능)

소요 시간 예상: 840편 × 병렬 4 = ~20시간
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from pipeline.config_loader import (  # noqa: E402
    PAPERS_DIR as _PAPERS_DIR,
    PIPELINE_DIR,
    _ssl_ctx,
    get_collection_key,
    get_collections,
    get_topic_dir,
    get_zotero_api_key,
    get_zotero_dir,
    get_zotero_user_id,
    load_config,
)
from pipeline.lib.generation_cache import (  # noqa: E402
    CacheFailure,
    CacheIdentity,
    CacheSuccess,
    GenerationCache,
    GenerationCacheError,
)
from pipeline.lib.run_state import (  # noqa: E402
    ResumeRequiredError,
    RunStatus,
    TopicBusyError,
)
from pipeline.geometry_figures import publish_geometry_manifest  # noqa: E402
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
from pipeline.update_geometry_orchestration import (  # noqa: E402
    Stage,
    acquire_plan_lease,
    approved_python,
    artifact_manifest,
    build_transitional_sparse_index,
    default_stage_plan,
    execute_plan,
    safe_child_environment,
)
from pipeline.release_dry_run import (  # noqa: E402
    ArtifactValidationError,
    build_dry_run_plan,
    build_policy_denied_result,
    emit as emit_release_result,
    validate_default_artifacts,
)
PAPERS_DIR = str(_PAPERS_DIR)
PROJECT_ROOT = PIPELINE_DIR.parent


def _topic_default_artifacts_missing(topic):
    """Return missing default topic outputs for a complete collection page.

    Paper Curio collection processing is an "ensure the collection is browsable"
    entrypoint, not just "review new PDFs". A failed first run can leave the
    checkpoint full while narrative/timeline artifacts are absent; resume mode
    must detect that and rebuild the missing default outputs.
    """
    topic_dir = get_topic_dir(topic)
    topic_dir_str = str(topic_dir)
    missing = []
    required = [
        "_category_summaries.json",
        "_category_narratives.json",
        "_timeline_narrative.json",
    ]
    for name in required:
        path = os.path.join(topic_dir_str, name)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            missing.append(name)

    return missing

# topic_modeling.py / classify_papers.py 는 UMAP + hdbscan + sentence-transformers
# 의존. 표준은 ``py312`` 단일 env 이며, 이 경우 현재 인터프리터가 곧 클러스터링
# 인터프리터다. 과거의 "py314 메인 + py312 보조" 이중 구성도 계속 지원한다: py314
# 에서는 UMAP.transform 가 sklearn.pairwise_distances 에 callable metric 을 넘기는
# 순간 numba 가 JIT 컴파일을 시도하는데, numba 의 bytecode interpreter 가 Python
# 3.14 의 ``CALL_KW`` opcode 를 아직 처리하지 못해
# (``op_CALL_KW: pop from empty list``, 0.65.1 / 0.66.0rc1 / main 모두 동일) 분류가
# 죽으므로, 클러스터링·분류만 형제 ``py312`` 인터프리터로 라우팅한다.
#
# 해석 순서:
#   0. 현재 인터프리터 프로브 — 클러스터링 *실경로* (UMAP cosine fit→transform +
#      hdbscan approximate_predict) 를 소형 데이터로 실행하는 서브프로세스가
#      성공하면 단일 env 로 보고 ``sys.executable`` 을 그대로 쓴다. import-only
#      프로브는 JIT 시점 크래시를 못 잡아 거짓 양성을 내므로 쓰지 않는다.
#      판정은 _state/env_probe.json 에 영구 캐시 (첫 1회만 JIT 비용 ~수십 초).
#   프로브 실패(의존성 미설치 또는 JIT 크래시) 시 기존 fallback 체인을 그대로 탄다:
#   1. ``PAPER_CURATION_PY312`` 환경변수 — 운영자가 명시한 절대 경로
#   2. 현재 인터프리터의 conda prefix 에서 형제 env (../py312/bin/python)
#   3. ``which python3.12`` PATH 검색
#   4. fallback → ``sys.executable`` (운영자 환경이 이미 py312 이면 동작)
_PROBE_RESULT = None  # None=미실행, True=현재 인터프리터로 클러스터링 가능, False=불가

# 프로브는 import 가 아니라 *실제 크래시 경로* 를 실행한다. numba CALL_KW 류의
# 실패는 import 시점이 아니라 UMAP.transform()/approximate_predict() 의 JIT
# 컴파일 시점에 터지므로, import-only 프로브는 py314 에서 거짓 양성을 낸다
# (import 성공 → 단일 env 판정 → 분류 단계 mid-run 크래시). 아래 스니펫은
# topic_modeling/classify 가 쓰는 경로(UMAP cosine fit→transform,
# hdbscan approximate_predict)를 소형 데이터로 그대로 통과시킨다.
# 첫 실행은 numba JIT 때문에 수십 초 걸리므로 판정을 _state/env_probe.json 에
# (인터프리터 경로, 파이썬 버전) 키로 영구 캐시한다. env 의 numba 를 갈아끼운
# 뒤 재판정이 필요하면 그 파일을 지우면 된다.
_PROBE_SNIPPET = (
    # sentence_transformers/adapters 까지 요구하는 이유: topic_modeling 은 SPECTER2
    # 임베딩이 필수이고, adapters 가 없으면 proximity 어댑터 대신 mean-pooling
    # fallback 으로 *조용히 품질이 떨어진* 번들을 만든다. 어댑터까지 갖춘 env 만
    # "단일 env" 로 인정해 EMBED_TAG(proximity vs fallback)가 env 에 따라 갈라지는
    # 것을 막는다. 모두 없는 환경이라도 fallback 체인의 마지막이 sys.executable
    # 이므로 동작 자체는 유지된다(이때는 specter2_embed 가 경고와 함께 fallback).
    "import sentence_transformers, adapters\n"
    "import numpy as np, numba, umap, hdbscan\n"
    "rs = np.random.RandomState(0)\n"
    "X = rs.rand(60, 32).astype('float32')\n"
    "um = umap.UMAP(n_components=5, n_neighbors=8, metric='cosine',"
    " random_state=42).fit(X)\n"
    "um.transform(rs.rand(3, 32).astype('float32'))\n"
    "cl = hdbscan.HDBSCAN(min_cluster_size=3, prediction_data=True)"
    ".fit(um.embedding_.astype('float64'))\n"
    "import hdbscan.prediction as hp\n"
    "hp.approximate_predict(cl, um.transform(rs.rand(2, 32).astype('float32'))"
    ".astype('float64'))\n"
    "print('PROBE_OK numba', numba.__version__)\n"
)
import pathlib as _pathlib
_PROBE_STATE = _pathlib.Path(__file__).resolve().parent / "_state" / "env_probe.json"


def _probe_current_interpreter():
    """현재 인터프리터로 클러스터링 실경로가 도는지 1회 판정 (영구 캐시).

    의존성 미설치(즉시 ImportError)·JIT 크래시·타임아웃 모두 False.
    판정은 프로세스 전역 + ``_state/env_probe.json`` 에 캐시된다.
    """
    global _PROBE_RESULT
    if _PROBE_RESULT is not None:
        return _PROBE_RESULT

    key = f"{sys.executable}|py{sys.version_info[0]}.{sys.version_info[1]}"
    try:
        state = json.loads(_PROBE_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    cached = state.get(key)
    if isinstance(cached, dict) and "ok" in cached:
        _PROBE_RESULT = bool(cached["ok"])
        return _PROBE_RESULT

    try:
        r = subprocess.run(
            [sys.executable, "-c", _PROBE_SNIPPET],
            capture_output=True, text=True, timeout=180,
        )
        ok = (r.returncode == 0 and "PROBE_OK" in (r.stdout or ""))
        detail = (r.stdout if ok else (r.stderr or ""))[-200:].strip()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:120]}"

    _PROBE_RESULT = ok
    try:
        state[key] = {"ok": ok, "detail": detail,
                      "ts": datetime.now().isoformat(timespec="seconds")}
        _PROBE_STATE.parent.mkdir(parents=True, exist_ok=True)
        _PROBE_STATE.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # 캐시 실패는 치명적이지 않음 (다음 실행에서 재프로브)
    return _PROBE_RESULT


def _resolve_topic_modeling_python():
    # 표준: py312 단독 환경. py314 등 다른 인터프리터로는 **절대 라우팅하지 않는다**.
    # (운영자 지시 2026-06-18: py312 단독 사용, py314 진입 경로 전면 차단.)
    # 0. 현재 인터프리터가 이미 py312 + 클러스터링 의존성을 직접 import 할 수 있으면
    #    그대로 사용 (단일 env 표준 경로).
    from pathlib import Path as _Path
    import shutil as _shutil
    if sys.version_info[:2] == (3, 12) and _probe_current_interpreter():
        return sys.executable
    # 그 외(예: py314 에서 호출) → py312 인터프리터로 라우팅. py314 로는 절대 가지 않는다.
    #   1) PAPER_CURATION_PY312 명시 경로
    explicit = os.environ.get("PAPER_CURATION_PY312", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    #   2) 형제 conda env <base>/envs/py312/bin/python
    here = _Path(sys.executable).resolve()
    for anc in here.parents:
        if anc.name == "envs":
            cand = anc / "py312" / "bin" / "python"
            if cand.exists():
                return str(cand)
            break
    #   3) PATH 의 python3.12
    found = _shutil.which("python3.12")
    if found:
        return found
    #   4) 현재 인터프리터가 py312 면 그대로 (의존성 미설치 상태일 수 있음)
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    # py312 를 못 찾으면 py314 로 진행하지 않고 명확히 실패한다.
    raise RuntimeError(
        "py312 인터프리터를 찾을 수 없습니다. paper-curation 은 py312 단독 환경을 사용합니다 "
        "(py314 사용 금지). conda env py312 를 만들거나 PAPER_CURATION_PY312 로 절대 경로를 지정하세요."
    )


TOPIC_MODELING_PYTHON = sys.executable
ZOTERO_DIR = ""
API_KEY = ""
USER_ID = ""
COLLECTIONS: dict[str, str] = {}

# Checkpoint
CHECKPOINT_FILE = str(PIPELINE_DIR / "_update_force_checkpoint.json")


def _initialize_runtime(policy: RuntimePolicy) -> RuntimePolicy:
    """Initialize credentials only after an already-resolved Codex policy."""
    global API_KEY, COLLECTIONS, TOPIC_MODELING_PYTHON, USER_ID, ZOTERO_DIR
    TOPIC_MODELING_PYTHON = _resolve_topic_modeling_python()
    if TOPIC_MODELING_PYTHON != sys.executable:
        print(
            f"[env] UMAP/HDBSCAN 단계 인터프리터: {TOPIC_MODELING_PYTHON} "
            "(현재 env 가 py312 가 아니거나 프로브 실패 → py312 라우팅; "
            "사유는 _state/env_probe.json)"
        )
    ZOTERO_DIR = get_zotero_dir()
    API_KEY = get_zotero_api_key()
    USER_ID = get_zotero_user_id()
    COLLECTIONS = get_collections()
    return policy


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_checkpoint():
    # A truncated/corrupt checkpoint (process killed mid-flush) must NOT crash
    # the run before any work begins — degrade to the empty default instead.
    # The --skip-existing/PDF-mtime scan re-marks slugs with a valid review.md
    # as completed, so most prior work is recovered for free.
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log(f"  WARN checkpoint corrupt ({type(e).__name__}); "
                f"starting fresh — completed papers re-detected from review.md")
    return {"completed": [], "failed": [], "phase": "init"}


_cp_lock = threading.Lock()
_slug_to_zotero_key = {}
_slug_to_pdf_path = {}  # populated by find_pdf hits; persisted to _papers_index.json for PDF-change detection on subsequent runs


def save_checkpoint(cp):
    # Atomic write (tmp + os.replace) so a kill mid-flush leaves the reader the
    # old complete file rather than a half-written one. _cp_lock still
    # serializes concurrent writers AND the json serialization (callers may
    # append to cp['completed'] from other threads).
    from pipeline.lib.atomic_io import atomic_write_json
    with _cp_lock:
        atomic_write_json(CHECKPOINT_FILE, cp)


# ── Phase 1: Fetch Zotero ──

def fetch_zotero_items(collection_key):
    items = []
    start = 0
    while True:
        url = (f"https://api.zotero.org/users/{USER_ID}/collections/"
               f"{collection_key}/items/top?limit=100&start={start}&format=json&sort=title")
        # Korea-to-Zotero links drop connections mid-stream; retry with exp backoff.
        last_err = None
        batch = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"Zotero-API-Key": API_KEY})
                with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx) as resp:
                    batch = json.load(resp)
                break
            except Exception as e:
                last_err = e
                wait = min(60, 2 * (2 ** attempt))
                print(f"[fetch_zotero_items] attempt {attempt+1}/5 (start={start}) failed: "
                      f"{type(e).__name__}: {str(e)[:120]}; sleeping {wait}s", flush=True)
                import time as _t; _t.sleep(wait)
        if batch is None:
            raise RuntimeError(f"fetch_zotero_items exhausted retries at start={start}: {last_err}")
        if not batch:
            break
        for item in batch:
            d = item["data"]
            if d.get("itemType") in ("attachment", "note",
                                      "forumPost", "videoRecording"):
                continue
            items.append(d)
        start += 100
        if len(batch) < 100:
            break
    return items


# ── PDF match audit log (Phase 1a: diagnose 139-paper mismatch bug) ──
# Every find_pdf() call appends one JSON line to this file so we can
# retroactively audit which matches used the weak fuzzy fallback.
_AUDIT_LOG_PATH = None  # lazily initialised in _audit_append()


def _audit_append(record):
    """Append one JSONL record to the find_pdf audit log."""
    global _AUDIT_LOG_PATH
    try:
        if _AUDIT_LOG_PATH is None:
            logs_dir = os.path.join(os.path.dirname(__file__), "_logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _AUDIT_LOG_PATH = os.path.join(logs_dir, f"find_pdf_audit_{ts}.jsonl")
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit log never breaks the pipeline


def _extract_arxiv_id_from_item(item):
    """Try to read an arXiv ID from a Zotero item's url/DOI/archiveID."""
    for field in ("url", "DOI", "archiveID", "extra"):
        val = item.get(field, "") or ""
        m = re.search(r"(\d{4}\.\d{4,5})", val)
        if m:
            return m.group(1)
    return None


# Global flag toggled by CLI. When True, fuzzy fallback is disabled entirely.
_STRICT_PDF = False


def find_pdf(item):
    """Find local PDF path for a Zotero item.

    Returns (path, method). `method` is one of:
      - zotero_children_abs     : Zotero child API returned an absolute path that exists
      - zotero_children_basename: Zotero child API path's basename found under ZOTERO_DIR
      - doi_filename            : DOI string appears in a PDF filename under ZOTERO_DIR
      - arxiv_filename          : arXiv ID appears in a PDF filename under ZOTERO_DIR
      - fuzzy                   : weak title-keyword match (LAST RESORT, logged)
      - no_match                : no candidate — caller skips the paper
    """
    key = item.get("key", "")
    title = item.get("title", "")

    # Priority 1: Zotero children API (authoritative attachment path)
    try:
        url = f"https://api.zotero.org/users/{USER_ID}/items/{key}/children?format=json"
        req = urllib.request.Request(url, headers={"Zotero-API-Key": API_KEY})
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            children = json.load(resp)
        for c in children:
            cd = c.get("data", {})
            if cd.get("itemType") != "attachment":
                continue
            if cd.get("contentType") not in ("application/pdf", ""):
                continue
            path = cd.get("path", "") or cd.get("filename", "")
            if not path.lower().endswith(".pdf"):
                continue
            # Zotero "Linked Attachment Base Directory" stores paths as
            # `attachments:<filename>` (relative to ZOTERO_DIR). Strip the
            # prefix so os.path.exists/basename work against the real file.
            if path.startswith("attachments:"):
                rel = path[len("attachments:"):]
                resolved = os.path.join(ZOTERO_DIR, rel)
                if os.path.exists(resolved):
                    _audit_append({"key": key, "title": title[:80],
                                   "method": "zotero_children_attachments_uri",
                                   "path": resolved})
                    return resolved, "zotero_children_abs"
            if path and os.path.exists(path):
                _audit_append({"key": key, "title": title[:80],
                               "method": "zotero_children_abs", "path": path})
                return path, "zotero_children_abs"
            # Cross-platform basename extraction: Zotero may have stored a
            # Windows-style absolute path (``C:\Users\...\Foo.pdf``) when the
            # item was added on Windows, then the user re-attached the
            # collection on macOS via "Linked Attachment Base Directory".
            # ``os.path.basename`` on POSIX doesn't treat backslash as a
            # separator, so we'd otherwise treat the whole path as the
            # filename and never resolve it. Normalise to forward slashes
            # first so the basename works on either separator.
            fname = path.replace("\\", "/").rsplit("/", 1)[-1]
            for alt in (
                os.path.join(ZOTERO_DIR, fname),
                os.path.join(ZOTERO_DIR, "storage", cd.get("key", ""), fname),
            ):
                if os.path.exists(alt):
                    _audit_append({"key": key, "title": title[:80],
                                   "method": "zotero_children_basename", "path": alt})
                    return alt, "zotero_children_basename"
    except Exception as e:
        _audit_append({"key": key, "title": title[:80],
                       "method": "children_api_error", "error": str(e)[:200]})

    # Priority 2: DOI in filename (reliable when present)
    # Bare substring containment collides on prefix-DOIs ('10.1234/abc' is a
    # substring of '10.1234/abc.suppl'). Require the matched region to be
    # bounded — followed by end-of-name or a non-alphanumeric separator in the
    # ORIGINAL filename — and treat 2+ matches as ambiguous (no_match) since
    # os.listdir order is arbitrary.
    doi = (item.get("DOI") or item.get("doi") or "").strip()
    if doi:
        doi_norm = re.sub(r"[^a-z0-9]", "", doi.lower())
        if len(doi_norm) >= 10:  # avoid accidental short-DOI collisions
            doi_matches = []
            for fname in os.listdir(ZOTERO_DIR):
                if not fname.lower().endswith(".pdf"):
                    continue
                # Strip the .pdf extension BEFORE normalising, otherwise the
                # trailing 'pdf' makes the bounded check below see 'p' as the
                # next char and reject every match.
                stem = fname[:-4].lower()
                fname_norm = re.sub(r"[^a-z0-9]", "", stem)
                idx = fname_norm.find(doi_norm)
                if idx < 0:
                    continue
                # Bounded: the char right after the DOI in the normalised stem
                # must NOT be alphanumeric (else it's a longer DOI). Since
                # non-alphanumerics were stripped, a trailing char here means
                # the real filename had extra DOI digits/letters → reject.
                nxt = idx + len(doi_norm)
                if nxt < len(fname_norm) and fname_norm[nxt].isalnum():
                    continue
                doi_matches.append(fname)
            if len(doi_matches) == 1:
                matched = os.path.join(ZOTERO_DIR, doi_matches[0])
                _audit_append({"key": key, "title": title[:80],
                               "method": "doi_filename", "path": matched,
                               "doi": doi})
                return matched, "doi_filename"
            elif len(doi_matches) > 1:
                _audit_append({"key": key, "title": title[:80],
                               "method": "doi_ambiguous", "doi": doi,
                               "candidates": doi_matches[:5]})

    # Priority 3: arXiv ID in filename
    # Bare `arxiv_id in fname` makes '2401.0001' match '2401.00012' (wrong,
    # newer paper). Require a delimited match: no digit/dot immediately before
    # and no digit immediately after the id (an arXiv id may have a vN suffix,
    # so allow a following 'v'/non-digit). Ambiguous (2+) → no_match.
    arxiv_id = _extract_arxiv_id_from_item(item)
    if arxiv_id:
        arxiv_re = re.compile(rf"(?<![0-9.]){re.escape(arxiv_id)}(?![0-9])")
        arxiv_matches = [fname for fname in os.listdir(ZOTERO_DIR)
                         if fname.lower().endswith(".pdf") and arxiv_re.search(fname)]
        if len(arxiv_matches) == 1:
            matched = os.path.join(ZOTERO_DIR, arxiv_matches[0])
            _audit_append({"key": key, "title": title[:80],
                           "method": "arxiv_filename", "path": matched,
                           "arxiv_id": arxiv_id})
            return matched, "arxiv_filename"
        elif len(arxiv_matches) > 1:
            _audit_append({"key": key, "title": title[:80],
                           "method": "arxiv_ambiguous", "arxiv_id": arxiv_id,
                           "candidates": arxiv_matches[:5]})

    # Priority 4 (optional): fuzzy title match — STRICTER than before.
    # The original 3-of-5 keyword rule caused the 139-paper mismatch bug.
    # We now require: (a) at least 5 significant keywords from the title,
    # (b) at least 5 of them appear in the filename (score >= 5), and
    # (c) coverage >= 80% of candidate keywords. Disabled entirely under
    # --strict-pdf.
    if _STRICT_PDF or not title:
        _audit_append({"key": key, "title": title[:80], "method": "no_match",
                       "reason": "strict_pdf" if _STRICT_PDF else "no_title"})
        return None, "no_match"

    title_words = re.sub(r"[^a-z0-9\s]", "", title.lower()).split()
    key_words = [w for w in title_words if len(w) > 3][:8]
    if len(key_words) < 5:
        _audit_append({"key": key, "title": title[:80], "method": "no_match",
                       "reason": "too_few_keywords", "keywords": key_words})
        return None, "no_match"

    best_match = None
    best_score = 0
    for fname in os.listdir(ZOTERO_DIR):
        if not fname.lower().endswith(".pdf"):
            continue
        fname_lower = fname.lower()
        score = sum(1 for w in key_words if w in fname_lower)
        if score > best_score:
            best_score = score
            best_match = fname

    coverage = best_score / max(1, len(key_words))
    if best_match and best_score >= 5 and coverage >= 0.8:
        matched = os.path.join(ZOTERO_DIR, best_match)
        _audit_append({"key": key, "title": title[:80], "method": "fuzzy",
                       "path": matched, "score": best_score,
                       "coverage": round(coverage, 2), "keywords": key_words,
                       "WARNING": "weak match — verify with audit_matching.py"})
        log(f"  WARN fuzzy PDF match for '{title[:60]}' "
            f"(score {best_score}/{len(key_words)}, cov {coverage:.0%})")
        return matched, "fuzzy"

    _audit_append({"key": key, "title": title[:80], "method": "no_match",
                   "reason": "fuzzy_below_threshold", "best_score": best_score,
                   "best_candidate": best_match, "keywords": key_words})
    return None, "no_match"


# ── Zotero ↔ text.md sanity check (zero-cost PDF integrity gate) ──

def _zotero_text_sanity(item, text_md_path, min_title_coverage=0.6):
    """Verify Zotero metadata (title/DOI/first author) appears in the extracted
    text. Catches the "Zotero attachment is wrong PDF" failure mode that even
    `--strict-pdf` cannot detect: find_pdf returned Zotero's linked file, but
    that file is the wrong paper.

    All signals are local regex/substring — no LLM call.

    Returns (passed: bool, reason: str).
    """
    if not os.path.exists(text_md_path):
        return True, "no_text_skip"  # text extraction failed; downstream already fails
    try:
        with open(text_md_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(6000).lower()
    except Exception:
        return True, "read_error_skip"

    # 1. Title keyword coverage
    # Include Hangul syllables so Korean titles like
    # "BiCoord: 장기간 시공간 협응 양팔 조작 벤치마크" yield several keywords
    # rather than just one English fragment. ASCII tokens still need >3
    # chars to skip stopwords; Hangul tokens use a ≥2-char threshold
    # since Korean morphemes are short.
    title_lower = (item.get("title") or "").lower()
    title_words = re.sub(r"[^a-z0-9가-힣\s]", " ", title_lower).split()
    kw = [w for w in title_words
          if (w.isascii() and len(w) > 3) or (not w.isascii() and len(w) >= 2)
          ][:6]
    title_hits = sum(1 for w in kw if w in text) if kw else 0
    # Scale threshold to kw length: 60% coverage with a floor of 1. The
    # original ``max(3, ...)`` rejected legitimate matches when titles
    # produced fewer than 5 keywords (typical for Korean papers and
    # short titles).
    threshold = max(1, int(len(kw) * min_title_coverage))
    title_ok = (not kw) or title_hits >= threshold

    # ASCII-only fallback: covers the "Korean title + English PDF" case
    # where the user registered a translated title in Zotero but the
    # actual PDF body is the English original. The Hangul keywords will
    # never appear in the English text, so we additionally pass if the
    # ASCII subset of the keywords has full coverage (≥60% with floor 1).
    if not title_ok and kw:
        ascii_kw = [w for w in kw if w.isascii()]
        if ascii_kw:
            ascii_hits = sum(1 for w in ascii_kw if w in text)
            ascii_thr = max(1, int(len(ascii_kw) * min_title_coverage))
            if ascii_hits >= ascii_thr:
                title_ok = True

    # 2. DOI exact (ignore non-alphanumeric)
    doi = re.sub(r"[^a-z0-9]", "", (item.get("DOI") or "").lower())
    text_norm = re.sub(r"[^a-z0-9]", "", text)
    doi_ok = True if len(doi) < 10 else (doi in text_norm)

    # 3. First author lastName
    creators = item.get("creators", []) or []
    last = ""
    for c in creators:
        last = (c.get("lastName") or "").lower()
        if last:
            break
    author_ok = True if len(last) < 3 else (last in text)

    # title + doi 둘 다 통과면 author 무관 (corporate author, 이미지 헤더 등
    # 저자명 추출 실패가 흔해서 단독 blocker로 쓰지 않음).
    if title_ok and doi_ok:
        return True, f"ok (title {title_hits}/{len(kw)} doi=T author={author_ok})"
    if title_ok and author_ok:
        return True, f"ok (title {title_hits}/{len(kw)} author=T doi={doi_ok})"
    return False, (f"title_hits={title_hits}/{len(kw)} "
                   f"doi_ok={doi_ok} author_ok={author_ok}")


# ── Phase 2: Extract text.md (OpenDataLoader → PyMuPDF fallback) ──

def extract_text(pdf_path, slug_dir):
    text_path = os.path.join(slug_dir, "text.md")

    # Strategy 1: OpenDataLoader (better table/formula/structure extraction)
    # NOTE: pip package is `opendataloader-pdf` but Python import name is
    # `opendataloader_pdf` (underscore). Earlier code used `opendataloader.pdf`
    # which silently ImportError'd and fell through to PyMuPDF every time.
    # Requires Java Runtime (e.g. `brew install --cask temurin`).
    try:
        import tempfile, re
        from opendataloader_pdf import convert as odl_convert
        with tempfile.TemporaryDirectory() as tmpdir:
            odl_convert(input_path=[pdf_path], output_dir=tmpdir,
                        format="markdown", quiet=True)
            md_files = [f for f in os.listdir(tmpdir) if f.endswith(".md")]
            if md_files:
                with open(os.path.join(tmpdir, md_files[0]), "r", encoding="utf-8") as f:
                    text = f.read()
                if text and len(text) > 100:
                    # OpenDataLoader exports its own image dump and embeds
                    # `![image N](relative/path.png)` lines pointing to a
                    # sibling dir that paper-curation never tracks (we use
                    # PyMuPDF for figures/). Collapse those refs into
                    # `[Figure N]` so the body flow survives but the broken
                    # paths don't pollute Claude review prompts.
                    text = re.sub(r'!\[image\s*(\d+)\]\([^)]*\)',
                                  r'[Figure \1]', text)
                    text = re.sub(r'\n{3,}', '\n\n', text)
                    with open(text_path, "w", encoding="utf-8") as f:
                        f.write(text)
                    return True
    except ImportError:
        pass  # OpenDataLoader not installed → fallback
    except Exception as e:
        log(f"  OpenDataLoader failed: {e}, falling back to PyMuPDF")

    # Strategy 2: PyMuPDF fallback
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = "\n".join(doc[p].get_text() for p in range(len(doc)))
        doc.close()
        if text and len(text) > 100:
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(text)
            return True
    except Exception as e:
        log(f"  text.md failed: {e}")
    return False


# ── Phase 3: Extract figures + local geometric validation ──

# Figure crop is LOCALIZED FROM THE PDF'S OWN GEOMETRY before any LLM call.
# The previous design seeded every crop as the full page and relied on a remote
# validator to shrink it; a validator failure or an "ok" on round 0 left the
# ENTIRE PAGE as the figure. We now build the crop box from the union of the
# graphic rects (raster image blocks + get_image_rects + area-filtered
# get_drawings) that are adjacent to the caption, render THAT box first, and
# and keeps all later refinement within those bounds. Refinement can never
# enlarge the box back to a full page, and an unavailable validator keeps the geometric
# box instead of falsely accepting "ok".


def _union_rects(rects):
    """Union a list of (x0,y0,x1,y1) tuples into one bounding box, or None."""
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    return [x0, y0, x1, y1]


def _collect_graphic_rects(page, pw, ph):
    """Gather candidate graphic rects on a page from three sources:
      1. raster image blocks (text_dict type==1)
      2. page.get_image_rects() for every embedded image xref
      3. page.get_drawings() vector paths (area-filtered, capped per page)
    Returns list of (x0,y0,x1,y1). Tiny rects (axis-tick fragments, hairlines)
    are dropped so a vector-heavy page doesn't explode the union.
    """
    MIN_W, MIN_H = 40, 40  # ignore sub-glyph fragments
    rects = []

    # Source 1: raster image blocks
    try:
        td = page.get_text("dict")
        for b in td["blocks"]:
            if b.get("type") == 1:
                bb = b["bbox"]
                if (bb[2] - bb[0]) > MIN_W and (bb[3] - bb[1]) > MIN_H:
                    rects.append((bb[0], bb[1], bb[2], bb[3]))
    except Exception:
        pass

    # Source 2: get_image_rects() — embedded raster placement (handles repeats)
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                for r in page.get_image_rects(xref):
                    if (r.x1 - r.x0) > MIN_W and (r.y1 - r.y0) > MIN_H:
                        rects.append((r.x0, r.y0, r.x1, r.y1))
            except Exception:
                continue
    except Exception:
        pass

    # Source 3: get_drawings() — vector plots/schematics have no raster block.
    # This can return thousands of tiny paths; filter by area and cap work.
    try:
        drawings = page.get_drawings()
        page_area = max(1.0, pw * ph)
        count = 0
        for d in drawings:
            count += 1
            if count > 4000:  # hard cap so vector-dense pages stay fast
                break
            r = d.get("rect")
            if r is None:
                continue
            w = r.x1 - r.x0
            h = r.y1 - r.y0
            if w <= MIN_W or h <= MIN_H:
                continue
            # Drop near-page-spanning full-bleed background rects (a single
            # path covering >95% of the page is a page border, not a figure).
            if (w * h) / page_area > 0.95:
                continue
            rects.append((r.x0, r.y0, r.x1, r.y1))
    except Exception:
        pass

    return rects


def extract_figures(pdf_path, slug_dir):
    fig_dir = os.path.join(slug_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)

    try:
        import fitz
    except ImportError:
        log("  PyMuPDF not available")
        return []

    doc = fitz.open(pdf_path)
    MARGIN = 30
    MAX_ROUNDS = 0

    # Collect caption candidates across all pages first, so we can dedup a
    # fig_num globally by ADJACENT-GRAPHIC AREA (prefer the real caption over a
    # body-text mention on another page) and cap EMITTED figures by area.
    candidates = []  # each: dict(fig_num, page, caption, box, hull, graphic_area)
    orphan_caps = []  # 캡션은 있는데 소유 그래픽이 없는 경우 — 페이지 영역 폴백 후보

    for pn in range(min(30, len(doc))):
        page = doc[pn]
        pw, ph = page.rect.width, page.rect.height
        # Page rotation: get_text bboxes and get_pixmap clip both operate in
        # the rotated coordinate space PyMuPDF reports via page.rect, so no
        # manual rotation maths is needed — page.rect already reflects it.
        text_dict = page.get_text("dict")
        txt_blocks = [b for b in text_dict["blocks"] if b["type"] == 0]

        graphic_rects = _collect_graphic_rects(page, pw, ph)

        # Scanned-PDF guard: a single image covering >90% of the page IS the
        # figure — keep the whole page in that legitimate case.
        full_page_image = None
        for r in graphic_rects:
            if ((r[2] - r[0]) * (r[3] - r[1])) / max(1.0, pw * ph) > 0.90:
                full_page_image = r
                break

        # Pass 1: collect EVERY caption line on the page first, so each
        # graphic rect can be assigned to the caption it actually belongs to.
        # A caption is the first line of a text block that STARTS with
        # "Figure/Fig/FIG N". Scan all lines of the block (Nature/Science merge
        # a trailing fragment above the caption into one block, so it may be
        # line #1+). Case-insensitive for physics/PRL "FIG. 1.".
        caps = []  # each: dict(fig_num, caption, x0, top, x1, bottom)
        for tb in txt_blocks:
            for ln in tb["lines"]:
                lt = "".join(s["text"] for s in ln["spans"]).strip()
                m = re.match(r"(?:Figure|Fig\.?)\s*([0-9]+)", lt, re.I)
                if m:
                    lb = ln["bbox"]
                    caps.append({
                        "fig_num": int(m.group(1)), "caption": lt[:120],
                        "x0": lb[0], "top": lb[1], "x1": lb[2],
                        "bottom": max(lb[3], tb["bbox"][3]),
                        # 캡션다움: 번호 뒤 구두점("Figure 1." / "Fig. 1:" 등).
                        # 본문 언급("Fig. 1 shows …")과 구분하는 신호 — dedup에서
                        # 진짜 캡션이 본문 언급에게 밀리지 않게 한다.
                        "caplike": bool(re.match(
                            r"(?:Figure|Fig\.?)\s*[0-9]+\s*[.:|)\]—–-]", lt, re.I)),
                    })
                    break
        if not caps:
            continue

        # Figures sit ABOVE their caption (dominant convention), so a tall
        # figure's TOP panels can be far above the caption — allow a generous
        # gap there. The figure-BELOW-caption case is rare; keep its gap tight
        # so the block of body text under a caption isn't vacuumed into the crop.
        gap_above = ph * 0.6
        gap_below = ph * 0.12

        # Pass 2: assign each graphic rect to its OWNING caption. The figure
        # width is derived from the rects themselves, NEVER from the caption's
        # column — a single-column caption legitimately owns a full-width
        # figure's far panels (common in Nature/Science). Ranking, best first:
        #   (1) caption BELOW the rect (figure-above-caption, the dominant
        #       convention) beats a caption above it;
        #   (2) a caption that horizontally OVERLAPS the rect beats one that
        #       does not — this keeps SIDE-BY-SIDE figures separate;
        #   (3) nearest vertical gap.
        assigned = {id(c): [] for c in caps}
        if full_page_image is not None:
            assigned[id(caps[0])].append(full_page_image)
        else:
            for r in graphic_rects:
                rx0, ry0, rx1, ry1 = r
                best_key = None
                best_cap = None
                for c in caps:
                    if c["top"] >= ry1 - 2 and (c["top"] - ry1) <= gap_above:
                        direction, vgap = 0, c["top"] - ry1   # figure above caption
                    elif c["bottom"] <= ry0 + 2 and (ry0 - c["bottom"]) <= gap_below:
                        direction, vgap = 1, ry0 - c["bottom"]  # figure below caption
                    else:
                        continue
                    overlap = not (rx1 <= c["x0"] or rx0 >= c["x1"])
                    key = (direction, 0 if overlap else 1, vgap)
                    if best_key is None or key < best_key:
                        best_key, best_cap = key, c
                if best_cap is not None:
                    assigned[id(best_cap)].append(r)

        for c in caps:
            adj_rects = assigned[id(c)]
            if not adj_rects:
                # No graphic content owned by this caption → body-text mention
                # or un-extractable figure. 과거엔 DROP했지만, 등록 시 '모든
                # 그림' 보장을 위해 페이지 영역 폴백 후보로 보관한다 (최종
                # 렌더는 함수 끝의 완성 단계).
                above = [cc["bottom"] for cc in caps if cc["bottom"] < c["top"] - 4]
                below = [cc["top"] for cc in caps if cc["top"] > c["bottom"] + 4]
                orphan_caps.append({
                    "fig_num": c["fig_num"], "page": pn, "caption": c["caption"],
                    "top": c["top"], "bottom": c["bottom"],
                    "caplike": c["caplike"],
                    "floor": (max(above) + 4) if above else 0.0,
                    "ceil": (min(below) - 4) if below else ph,
                    "pw": pw, "ph": ph,
                })
                continue
            hull = _union_rects(adj_rects)
            graphic_area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in adj_rects)
            # Crop box = union of owned rects + MARGIN. No caption-column clip.
            bx0 = max(0, hull[0] - MARGIN)
            by0 = max(0, hull[1] - MARGIN)
            bx1 = min(pw, hull[2] + MARGIN)
            by1 = min(ph, hull[3] + MARGIN)
            # Extend the crop toward the caption so TEXT-rendered figure content
            # (panel labels, chemical-formula lists, axis labels) between the
            # last graphic rect and the caption is not clipped. The caption is
            # the figure's hard boundary, so this can never reach body text.
            n_above = sum(1 for r in adj_rects if r[3] <= c["top"] + 2)
            if n_above * 2 >= len(adj_rects):     # figure above its caption
                by1 = min(ph, max(by1, c["top"] - 2))
            else:                                 # figure below its caption (rare)
                by0 = max(0, min(by0, c["bottom"] + 2))
            candidates.append({
                "fig_num": c["fig_num"], "page": pn, "caption": c["caption"],
                "caplike": c["caplike"],
                "box": [bx0, by0, bx1, by1],
                "hull": hull, "graphic_area": graphic_area,
                "pw": pw, "ph": ph,
            })

    # Page-scoped dedup: when the same fig_num appears on multiple pages,
    # prefer the candidate with the LARGEST adjacent graphic area (the real
    # caption, not a bare mention). Then cap EMITTED figures to top-5 by area.
    best_by_num = {}
    for c in candidates:
        prev = best_by_num.get(c["fig_num"])
        if prev is None or (
            (c.get("caplike", False), c["graphic_area"])
            > (prev.get("caplike", False), prev["graphic_area"])
        ):
            best_by_num[c["fig_num"]] = c
    # Cap emitted figures to the LOWEST 5 figure numbers. Main-body figures
    # (1-5) come before appendix figures, and in some papers (e.g. NLP papers
    # with many "Prompt for ..." boxes in the appendix) the appendix figures
    # have far larger area and would crowd out the real figures under an
    # area-based cap. Figure number is the more reliable importance signal.
    all_nums = sorted(best_by_num.values(), key=lambda c: c["fig_num"])
    # review 임베드 후보는 기존과 동일하게 top-5. 6번 이후는 아래 완성 단계에서
    # geometric-only로 저장만 한다 (등록 시 '모든 그림' 보관 — 채팅 인라인 그림용).
    chosen = all_nums[:5]
    extras = all_nums[5:20]

    figures = []
    for c in chosen:
        fig_num = c["fig_num"]
        pn = c["page"]
        caption = c["caption"]
        page = doc[pn]
        pw, ph = c["pw"], c["ph"]
        hull = c["hull"]
        x0, y0, x1, y1 = c["box"]
        out = os.path.join(fig_dir, f"fig{fig_num}.png")

        # Clamp bounds = the geometric box itself (owned-rect hull + MARGIN,
        # already extended toward the caption to capture text-rendered figure
        # content). Any local refinement stays within this box —
        # the geometric crop is the prior, so later processing
        # can neither escape to a full page nor re-clip the caption extension.
        hx0, hy0, hx1, hy1 = c["box"]

        rendered_ok = False
        for rnd in range(MAX_ROUNDS + 1):
            # Inverted-box guard: revert to the geometric box.
            if x1 <= x0 or y1 <= y0:
                x0, y0, x1, y1 = c["box"]

            rendered = False
            for scale in [3, 2, 1]:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                          clip=fitz.Rect(x0, y0, x1, y1))
                    pix.save(out)
                    rendered = True
                    break
                except Exception:
                    continue
            if not rendered:
                break  # skip this figure entirely
            rendered_ok = True

            if rnd == MAX_ROUNDS:
                break

            result: dict[str, Any] = {"status": "error", "adjust_pt": {}}
            status = result.get("status")
            if status == "ok":
                break
            if status == "error":
                # Validator unavailable → keep the geometric box, do not
                # expand, break. Never treat as 'ok'.
                break

            # 'clipped' / 'oversized' / 'both': nudge within the hull ± MARGIN.
            damping = [0.8, 0.6][min(rnd, 1)]
            adj = result.get("adjust_pt", {})
            ny0 = y0 - adj.get("top", 0) * damping
            ny1 = y1 + adj.get("bottom", 0) * damping
            nx0 = x0 - adj.get("left", 0) * damping
            nx1 = x1 + adj.get("right", 0) * damping
            # Clamp to the rect hull (± MARGIN) — the box can never escape the
            # detected graphic region back to a full page.
            x0 = min(max(nx0, hx0), hx1)
            y0 = min(max(ny0, hy0), hy1)
            x1 = min(max(nx1, hx0), hx1)
            y1 = min(max(ny1, hy0), hy1)

        if not rendered_ok:
            continue

        # Plausibility floor: if the final box is implausibly small vs the
        # union of detected rects, expand to the hull + MARGIN (a tight figure
        # box — NEVER the whole page). No full-page re-render fallback.
        hull_area = (hull[2] - hull[0]) * (hull[3] - hull[1])
        box_area = (x1 - x0) * (y1 - y0)
        if hull_area > 0 and box_area < hull_area * 0.5:
            x0, y0, x1, y1 = hx0, hy0, hx1, hy1
            for scale in [3, 2, 1]:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                          clip=fitz.Rect(x0, y0, x1, y1))
                    pix.save(out)
                    break
                except Exception:
                    continue

        figures.append({"name": str(fig_num), "page": pn, "caption": caption})

    # ── 등록 시 '모든 그림' 보장 (채팅 인라인 그림 완전성) ──────────────
    # (a) 6번 이후 그림: geometric-only 저장.
    #     review 반환(figures)에는 넣지 않아 기존 임베드 동작 불변.
    for c in extras:
        out = os.path.join(fig_dir, f"fig{c['fig_num']}.png")
        page = doc[c["page"]]
        x0, y0, x1, y1 = c["box"]
        for scale in [3, 2, 1]:
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                      clip=fitz.Rect(x0, y0, x1, y1))
                pix.save(out)
                break
            except Exception:
                continue

    # (b) 캡션은 있는데 산출물이 없는/깨진 그림: 캡션 기준 페이지 영역 크롭
    #     폴백. figure-above-caption 관례(위 영역) 우선, 얕으면 아래 영역.
    #     본문 일부가 섞일 수 있는 최후 폴백이지만 '그림이 아예 안 나오는'
    #     상태를 없앤다 (예: 과거 9159 fig1 = 0-byte).
    def _fig_ok(p):
        return os.path.exists(p) and os.path.getsize(p) > 1024

    have = set()
    for c in all_nums:
        if _fig_ok(os.path.join(fig_dir, f"fig{c['fig_num']}.png")):
            have.add(c["fig_num"])
    # 렌더 실패/0-byte로 끝난 검출 후보도 폴백 대상으로 승격
    for c in all_nums:
        if c["fig_num"] in have:
            continue
        orphan_caps.append({
            "fig_num": c["fig_num"], "page": c["page"], "caption": c["caption"],
            "caplike": c.get("caplike", False),
            "top": min(c["ph"], c["box"][3] + 2),
            "bottom": min(c["ph"], c["box"][3] + 14),
            "floor": 0.0, "ceil": c["ph"], "pw": c["pw"], "ph": c["ph"],
        })
    done_orphans = set()
    for o in sorted(orphan_caps, key=lambda o: (o["fig_num"], not o.get("caplike", False), o["page"], o["top"])):
        num = o["fig_num"]
        if num in have or num in done_orphans or num > 20:
            continue
        page = doc[o["page"]]
        opw, oph = o["pw"], o["ph"]
        y0a = max(0.0, o["top"] - 0.55 * oph, o["floor"])
        y1a = max(0.0, o["top"] - 2)
        y0b = min(oph, o["bottom"] + 2)
        y1b = min(oph, o["bottom"] + 0.55 * oph, o["ceil"])
        if y1a - y0a >= 60:
            ry0, ry1 = y0a, y1a
        elif y1b - y0b >= 60:
            ry0, ry1 = y0b, y1b
        else:
            continue
        out = os.path.join(fig_dir, f"fig{num}.png")
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2),
                                  clip=fitz.Rect(12, ry0, opw - 12, ry1))
            pix.save(out)
        except Exception:
            continue
        if _fig_ok(out):
            done_orphans.add(num)
            log(f"  fig{num}: geometric 검출 실패 → 캡션 기준 페이지 영역 폴백")
        else:
            try:
                os.remove(out)
            except OSError:
                pass

    descriptor_by_number = {
        int(candidate["fig_num"]): candidate
        for candidate in all_nums
        if str(candidate.get("fig_num", "")).isdigit()
    }
    for orphan in orphan_caps:
        number = int(orphan.get("fig_num", 0))
        descriptor_by_number.setdefault(number, orphan)
    figures = []
    for number, descriptor in sorted(descriptor_by_number.items()):
        path = os.path.join(fig_dir, f"fig{number}.png")
        if _fig_ok(path):
            figures.append(
                {
                    "caption": str(descriptor.get("caption", "")),
                    "name": str(number),
                    "page": int(descriptor.get("page", 0)),
                }
            )

    doc.close()
    return figures


# ── Phase 4: Write review.md (Claude Haiku → JSON → Template) ──

REVIEW_TEMPLATE = """---
schema_version: v1
---

# {title}

> **저자**: {authors} | **날짜**: {date} | {ref_label}: {ref_link}

---

## Essence

{fig_essence}
{essence}

## Motivation

- **Known**: {known}
- **Gap**: {gap}
- **Why**: {why}
- **Approach**: {approach}

## Achievement

{fig_achievement}
{achievement}

## How

{fig_how}
{how}

## Originality

{originality}

## Limitation & Further Study

{limitation}

## Evaluation

- Novelty: {novelty}/5
- Technical Soundness: {tech}/5
- Significance: {significance}/5
- Clarity: {clarity}/5
- Overall: {overall}/5

**총평**: {verdict}
"""


REVIEW_SCHEMA_PATH = PIPELINE_DIR / "schemas" / "review-v1.json"
REVIEW_SCHEMA_VERSION = "review-v1"
REVIEW_PROMPT_VERSION = "review-prompt-v1"


def _load_review_schema() -> JsonObject:
    with REVIEW_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("review-v1 schema must be an object")
    return value


REVIEW_SCHEMA: JsonObject | None = None


def _review_schema() -> JsonObject:
    global REVIEW_SCHEMA
    if REVIEW_SCHEMA is None:
        REVIEW_SCHEMA = _load_review_schema()
    return REVIEW_SCHEMA


_REVIEW_STR_TAGS = ("essence", "known", "gap", "why", "approach", "achievement",
                    "how", "originality", "limitation", "verdict")
_REVIEW_INT_TAGS = ("fig_essence", "fig_achievement", "fig_how",
                    "novelty", "technical", "significance", "clarity", "overall")


def _salvage_review_data(data):
    """Recover a structured review when the model invoked `emit_review` but
    dumped the XML-tagged review into one field instead of filling the schema.

    Three observed claude-sonnet-5 failure modes:
      A) everything crammed into `essence` (with a trailing `</invoke>`);
      B) `essence` is clean but the rest is stuffed into `known`/`how`/
         `originality` using antml `<parameter name="x">…</parameter>` syntax;
      C) clean fields plus a `<parameter name="x">…</parameter>` blob (for
         known/gap/why/approach) appended inside `essence`.
    All render as empty sections + default 3/3/3/3/3 scores. Normalize the
    tool-parameter syntax and re-parse every field. No-op for clean responses.
    """
    d = dict(data or {})
    other = [t for t in _REVIEW_STR_TAGS if t != "essence"]

    def _norm(s):
        s = re.sub(r'<parameter name="([^"]+)">(.*?)</parameter>', r'<\1>\2</\1>',
                   s or "", flags=re.S)
        s = re.sub(r'<parameter name="([^"]+)">', r'<\1>', s)
        s = re.sub(r'</parameter[^>]*>', '', s)
        return s.replace("</invoke>", "")

    normed = {k: _norm(v) for k, v in d.items() if isinstance(v, str)}
    combined = "\n".join(normed.values())
    tagged = "</essence>" in combined or any(
        ("<%s>" % t) in combined or ("</%s>" % t) in combined for t in other)
    if not tagged:
        return data  # well-formed

    strip_tags = re.compile(
        r"</?(?:%s|fig_\w+|novelty|technical|significance|clarity|overall)>"
        % "|".join(_REVIEW_STR_TAGS))
    fixed = dict(d)

    er = normed.get("essence", "")
    if "<essence>" in er:
        m = re.search(r"<essence>(.*?)</essence>", er, re.S)
        essence = m.group(1) if m else er
    elif "</essence>" in er:
        essence = er.split("</essence>", 1)[0]
    else:
        essence = re.split(r"<(?:%s|fig_\w+)>" % "|".join(other), er, 1)[0]
    essence = strip_tags.sub("", essence).strip()
    if essence:
        fixed["essence"] = essence

    for t in other:
        m = re.search(r"<%s>(.*?)</%s>" % (t, t), combined, re.S)
        if m and m.group(1).strip():
            val = m.group(1)
        else:
            # carrier field: keep only the leading text before the first
            # leaked tag (handles `known` holding a `<parameter …>` blob, a
            # bare `</known>`, or a spilled `<gap>…` sequence).
            cur = normed.get(t, "")
            val = re.split(
                r"<(?:/?)(?:parameter|%s|fig_\w+|novelty|technical|"
                r"significance|clarity|overall)\b" % "|".join(_REVIEW_STR_TAGS),
                cur, 1, flags=re.S)[0]
        val = strip_tags.sub("", val).strip()
        if val:
            fixed[t] = val
    for t in _REVIEW_INT_TAGS:
        m = re.search(r"<%s>\s*(\d+)" % t, combined)
        if m:
            fixed[t] = int(m.group(1))
    return fixed


class ReviewValidationError(ValueError):
    """A structured review violated deterministic host-side invariants."""


def _validate_review_data(data: JsonObject) -> JsonObject:
    schema = _review_schema()
    validate_json(data, schema)
    required = schema.get("required")
    if not isinstance(required, list) or set(data) != set(required):
        raise ReviewValidationError("review fields must exactly match review-v1")
    for name in _REVIEW_STR_TAGS:
        value = data.get(name)
        if not isinstance(value, str) or re.search(r"[가-힣]", value) is None:
            raise ReviewValidationError(f"{name} must contain Korean narrative")
        if re.search(r"</?(?:parameter|invoke|[a-z_]+)>", value, re.I):
            raise ReviewValidationError(f"{name} contains leaked tool tags")
        if re.search(r"(?m)^##\s+", value):
            raise ReviewValidationError(f"{name} contains a top-level heading")
    for name in ("fig_essence", "fig_achievement", "fig_how"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
            raise ReviewValidationError(f"{name} must be an integer from 0 to 20")
    for name in ("novelty", "technical", "significance", "clarity", "overall"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ReviewValidationError(f"{name} must be an integer from 1 to 5")
    return data


def _atomic_publish_review(path: Path, content: str) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            _ = handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_review(
    item,
    slug_dir,
    figures,
    *,
    runtime_policy: RuntimePolicy | None = None,
    gateway: Any | None = None,
    cache: GenerationCache | None = None,
    identity: CacheIdentity | None = None,
):
    text_path = os.path.join(slug_dir, "text.md")
    if not os.path.exists(text_path):
        return False

    source_bytes = Path(text_path).read_bytes()
    paper_text = source_bytes.decode("utf-8")[:15000]

    title = item.get("title", "")
    authors = ", ".join(
        f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        for c in item.get("creators", [])
    )
    date = item.get("date", "")
    doi = item.get("DOI", "")
    abstract = item.get("abstractNote", "")

    fig_refs = ""
    for fig in figures:
        fig_refs += f"\n- Fig {fig['name']}: {fig['caption'][:80]}"

    try:
        prompt = (
            "논문을 분석하고 제공된 JSON Schema와 정확히 일치하는 JSON 리뷰를 반환하라.\n\n"
            "모든 narrative 필드는 한국어 서술. 단 Jargon — 기술 용어·모델명·데이터셋·"
            "알고리즘·수식·프레임워크·제품명 등 — 은 원문 그대로 유지하고 번역하지 "
            "말 것. 예: \"diffusion model을 사용한다\" (O), "
            "\"확산 모델(diffusion model)을 사용한다\" (X).\n\n"
            f"제목: {title}\n"
            f"Abstract: {abstract}\n"
            f"본문 (발췌): {paper_text[:12000]}\n"
            f"Figure 목록:{fig_refs}\n"
        )
        slug = os.path.basename(slug_dir.rstrip("/\\"))
        if identity is None:
            policy = runtime_policy or resolve_runtime_policy(
                cast(JsonObject, load_config())
            )
            if policy.mode != "codex":
                raise GenerationCacheError("policy-denied", "review generation requires codex mode")
            if gateway is None:
                gateway = CodexGateway.production(PROJECT_ROOT)
            identity = CacheIdentity.from_gateway(
                runtime_policy=policy,
                gateway=gateway,
                role="long_form",
                prompt_version=REVIEW_PROMPT_VERSION,
                prompt=prompt,
                schema_version=REVIEW_SCHEMA_VERSION,
                schema=_review_schema(),
                source=source_bytes,
                task_id=f"review:{slug}",
            )
        if gateway is None:
            gateway = CodexGateway.production(PROJECT_ROOT)
        generation_cache = cache or GenerationCache(Path(slug_dir) / ".llm_cache")

        def _generate():
            try:
                result = gateway.generate_json(
                    "long_form",
                    prompt,
                    _review_schema(),
                )
                return CacheSuccess(_validate_review_data(result))
            except (CodexGatewayError, ReviewValidationError, SchemaError, TypeError, ValueError):
                return CacheFailure("failed")

        data = _validate_review_data(
            generation_cache.get_or_generate(identity, _generate)
        )

        # Build figure insertions
        def fig_block(fig_num_str):
            try:
                n = int(fig_num_str)
            except (ValueError, TypeError):
                return ""
            if n == 0:
                return ""
            fig = next((f for f in figures if int(f["name"]) == n), None)
            if not fig:
                return ""
            cap = fig.get("caption", f"Figure {n}")
            return f"![Figure {n}](figures/fig{n}.png)\n\n*{cap}*\n"

        # DOI vs URL
        url = item.get("url", "")
        if doi:
            ref_label = "**DOI**"
            ref_link = f"[{doi}](https://doi.org/{doi})"
        elif url:
            ref_label = "**URL**"
            ref_link = f"[{url}]({url})"
        else:
            ref_label = "**DOI**"
            ref_link = "N/A"

        review_text = REVIEW_TEMPLATE.format(
            title=title, authors=authors, date=date,
            ref_label=ref_label, ref_link=ref_link,
            essence=data.get("essence", ""),
            fig_essence=fig_block(data.get("fig_essence", 0)),
            known=data.get("known", ""),
            gap=data.get("gap", ""),
            why=data.get("why", ""),
            approach=data.get("approach", ""),
            achievement=data.get("achievement", ""),
            fig_achievement=fig_block(data.get("fig_achievement", 0)),
            how=data.get("how", ""),
            fig_how=fig_block(data.get("fig_how", 0)),
            originality=data.get("originality", ""),
            limitation=data.get("limitation", ""),
            novelty=data.get("novelty", 3),
            # Tool-use schema field is `technical`; legacy SDK responses
            # used `tech` so we accept both during the migration window.
            tech=data.get("technical", data.get("tech", 3)),
            significance=data.get("significance", 3),
            clarity=data.get("clarity", 3),
            overall=data.get("overall", 3),
            verdict=data.get("verdict", ""),
        )

        review_path = Path(slug_dir) / "review.md"
        _atomic_publish_review(review_path, review_text.strip() + "\n")
        return True

    except Exception as e:
        log(f"  review.md failed: {e}")
        return False


def fix_python_list_literals(slug_dir):
    """Post-process: convert Python list literals ['a','b'] to markdown bullets."""
    review_path = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review_path):
        return
    with open(review_path, "r", encoding="utf-8") as f:
        content = f.read()
    if "['" not in content and '["' not in content:
        return
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        s = line.strip()
        if (s.startswith("[") and s.endswith("]") and
                ("'" in s or '"' in s) and len(s) > 20):
            # Parse by splitting on quote-comma-quote patterns
            inner = s[1:-1]  # remove [ ]
            # Split on ', ' or ", " patterns between items
            items = re.split(r"'\s*,\s*'|'\s*,\s*\"|\"'\s*,\s*'|\"'\s*,\s*\"", inner)
            if len(items) > 1:
                for item in items:
                    clean = item.strip().strip("'\"")
                    if clean:
                        new_lines.append(f"- {clean}")
                continue
        new_lines.append(line)
    new_content = "\n".join(new_lines)
    if new_content != content:
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(new_content)


def validate_review_format(slug_dir):
    """Post-process: review.md 포맷 검증 + 자동 수정."""
    review_path = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review_path):
        return
    with open(review_path, "r", encoding="utf-8") as f:
        content = f.read()
    issues = []

    # 1. 필수 섹션 존재 확인
    required = ["## Essence", "## Motivation", "## Achievement", "## How",
                 "## Originality", "## Limitation", "## Evaluation"]
    for sec in required:
        if sec not in content:
            issues.append(f"Missing section: {sec}")

    # 2. Python 리스트 리터럴 잔류
    if re.search(r"^\['.{20,}", content, re.MULTILINE):
        issues.append("Python list literal remaining")

    # 3. Evaluation 테이블 형식 잔류
    if "## Evaluation" in content:
        eval_part = content.split("## Evaluation")[-1][:500]
        if "| " in eval_part and "---" in eval_part:
            issues.append("Evaluation in table format")

    # 4. 빈 DOI 링크
    if "[](https://doi.org/)" in content:
        issues.append("Empty DOI link")

    # 5. 제목이 파일명
    title_m = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    if title_m and ".pdf" in title_m.group(1):
        issues.append(f"Title contains filename: {title_m.group(1)[:40]}")

    if issues:
        slug = os.path.basename(slug_dir)
        log(f"  FORMAT ISSUES in {slug}: {issues}")

    return issues


def fix_figure_paths(slug_dir):
    """Post-process: replace placeholder URLs and broken figure paths with actual figure files."""
    review_path = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review_path):
        return
    with open(review_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Build map of available figures
    fig_dir = os.path.join(slug_dir, "figures")
    fig_map = {}
    if os.path.isdir(fig_dir):
        for fname in os.listdir(fig_dir):
            m = re.match(r"fig(\d+)\.(png|webp)", fname)
            if m:
                fig_map[int(m.group(1))] = f"figures/{fname}"

    if not fig_map:
        return

    changed = False

    # Replace any external image URL `![alt](http(s)://...)` with the matching
    # local figure. Picks figure number from alt text ("Figure 3", "Fig.2",
    # "그림 1"); falls back to sequential assignment from available figures.
    seq_state = {"next": min(fig_map.keys())}
    sorted_keys = sorted(fig_map.keys())
    ext_replaced = 0

    def replace_external_image(match):
        nonlocal ext_replaced
        alt = match.group(1)
        target = None
        num_m = re.search(r"(?:Figure|Fig\.?|그림)\s*(\d+)", alt, re.IGNORECASE)
        if num_m:
            n = int(num_m.group(1))
            if n in fig_map:
                target = fig_map[n]
        if target is None:
            for k in sorted_keys:
                if k >= seq_state["next"]:
                    target = fig_map[k]
                    seq_state["next"] = k + 1
                    break
        if target is None:
            return match.group(0)
        ext_replaced += 1
        return f"![{alt}]({target})"

    new_content = re.sub(
        r"!\[([^\]]*)\]\(https?://[^)]+\)",
        replace_external_image,
        content,
    )
    if new_content != content:
        content = new_content
        changed = True

    # Legacy placeholder pattern fallback (cases without `![alt](...)` syntax,
    # e.g. inline `<img src=...>` shipped by an older prompt template).
    def replace_placeholder(match):
        url = match.group(0)
        fig_m = re.search(r"Fig[+_ ](\d+)", url)
        if fig_m:
            num = int(fig_m.group(1))
            if num in fig_map:
                return fig_map[num]
        if 1 in fig_map:
            return fig_map[1]
        return url

    if "placeholder" in content:
        new_content = re.sub(r"https://via\.placeholder\.com/[^\)]+", replace_placeholder, content)
        if new_content != content:
            content = new_content
            changed = True

    # Fix broken relative paths (e.g., figures/fig1.png when only .webp exists)
    def fix_ext(match):
        path = match.group(1)
        fig_m = re.match(r"figures/fig(\d+)\.(png|webp)", path)
        if fig_m:
            num = int(fig_m.group(1))
            if num in fig_map and fig_map[num] != path:
                return f"({fig_map[num]})"
        return match.group(0)

    new_content = re.sub(r"\((figures/fig\d+\.(?:png|webp))\)", fix_ext, content)
    if new_content != content:
        content = new_content
        changed = True

    if changed:
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(content)
        slug = os.path.basename(slug_dir)
        log(f"  {slug}: fixed figure paths")


def fix_evaluation_format(slug_dir):
    """Post-process: convert any remaining Evaluation tables to list format."""
    review_path = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review_path):
        return
    with open(review_path, "r", encoding="utf-8") as f:
        content = f.read()
    # If Evaluation has table rows, convert
    if "## Evaluation" in content and "| " in content.split("## Evaluation")[-1][:500]:
        eval_section = content.split("## Evaluation")[-1]
        scores = {}
        for label in ["Novelty", "Technical Soundness", "Significance", "Clarity", "Overall"]:
            m = re.search(rf'{label}\D*(\d+(?:\.\d+)?)\s*/\s*5', eval_section)
            if m:
                scores[label] = m.group(1)
        verdict_m = re.search(r'\*\*총평\*\*[:\s]*(.+?)(?:\n|$)', eval_section)
        verdict = verdict_m.group(1).strip() if verdict_m else ""

        new_eval = "\n## Evaluation\n\n"
        for label in ["Novelty", "Technical Soundness", "Significance", "Clarity", "Overall"]:
            new_eval += f"- {label}: {scores.get(label, '3')}/5\n"
        if verdict:
            new_eval += f"\n**총평**: {verdict}\n"

        before_eval = content.split("## Evaluation")[0]
        content = before_eval + new_eval
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(content)


# ── Phase 5: Convert to HTML ──

def convert_to_html(slug):
    try:
        # Import review_to_html from repo
        sys.path.insert(0, str(PIPELINE_DIR))
        from review_to_html import convert_review, detect_topic
        md_path = os.path.join(PAPERS_DIR, slug, "review.md")
        html_path = os.path.join(PAPERS_DIR, slug, "index.html")
        index_path = os.path.join(PAPERS_DIR, "_papers_index.json")
        if not os.path.exists(md_path):
            return False
        topic = detect_topic(slug, index_path)
        # convert_review's figure-existence guard resolves figures/figN.png
        # against this arg, so it MUST be the full slug directory — passing
        # the bare slug name made the check look under CWD and silently drop
        # every figure from the rendered HTML (review_to_html.py:697 and
        # validate_papers.py:497 both pass the full dir).
        slug_dir = os.path.join(PAPERS_DIR, slug)
        html = convert_review(md_path, topic, slug_dir)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    except Exception as e:
        log(f"  html failed: {e}")
        return False


# ── Process single paper ──

def make_slug(item, existing_slugs):
    """Match item to existing slug or generate a new one.

    Matching policy (Phase 4 collision fix):
      • Normalise title and slug-text by stripping non-alphanumerics and
        lowercasing.
      • Require the FULL 40-character prefix to match — the original
        25-character cutoff conflated different papers that shared their
        first few words (e.g. "A hierarchical framework for measuring
        scientific impact" vs "A Hierarchical Framework for Humanoid
        Locomotion with Supernumerary Limbs", both → `ahierarchical
        frameworkfor` at 25 chars).
      • Both sides must have ≥ 40 normalised chars to be eligible; short
        titles fall through to new-slug allocation rather than risking a
        false match.
    """
    title = item.get("title", "Unknown")
    norm_title = re.sub(r"[^a-z0-9]", "", title.lower())

    # Match against existing slugs. The compared prefix length is
    # ``min(40, min(len(a), len(b)))``: 40 chars when both sides are long
    # enough (rejects the "Hierarchical Framework for measuring/Humanoid"
    # false positive at chars 25-40), but the full overlap when either
    # title is shorter (matches "Robot Learning from Human Videos: A
    # Survey" — 35 normalised chars — against its own existing slug).
    # Require a 10-char floor so single-word titles can't claim each
    # other.
    if len(norm_title) >= 10:
        for s in existing_slugs:
            parts = s.split("_", 1)
            if len(parts) < 2:
                continue
            slug_text = re.sub(r"[^a-z0-9]", "", parts[1].lower())
            if len(slug_text) < 10:
                continue
            match_len = min(40, len(norm_title), len(slug_text))
            if match_len >= 10 and norm_title[:match_len] == slug_text[:match_len]:
                return s

    # No match → new slug
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[:60].strip()
    safe = safe.replace(" ", "_")
    max_num = 0
    for d in existing_slugs:
        m = re.match(r"(\d+)_", d)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"{max_num + 1:03d}_{safe}"


def paper_has_other_topics(slug):
    """Check if paper belongs to topics other than the current one."""
    index_path = os.path.join(PAPERS_DIR, "_papers_index.json")
    if not os.path.exists(index_path):
        return False
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            papers = json.load(f)
        for p in papers:
            if p.get("slug") == slug:
                topics = p.get("topics", [])
                return len(topics) > 1
    except Exception:
        pass
    return False


MAX_RETRIES = 3


def _do_process(item, slug, slug_dir, pdf_path, runtime_policy=None):
    """Single attempt: text.md → figures → review.md → index.html.
    Returns (status, reason) — status is 'ok' or 'fail'."""

    # Extract text
    log(f"  {slug}: extracting text...")
    extract_text(pdf_path, slug_dir)
    text_path = os.path.join(slug_dir, "text.md")
    if not os.path.exists(text_path) or os.path.getsize(text_path) < 100:
        return "fail", "text_extraction_failed"

    # Zotero ↔ text sanity check (zero-cost PDF integrity gate)
    ok, reason = _zotero_text_sanity(item, text_path)
    if not ok:
        log(f"  {slug}: SANITY FAIL ({reason}) — aborting review")
        return "fail", f"sanity_mismatch:{reason}"

    # Extract figures
    log(f"  {slug}: extracting figures...")
    try:
        figures = extract_figures(pdf_path, slug_dir)
    except Exception as error:
        log(f"  {slug}: geometry extraction failed ({type(error).__name__})")
        figures = []
    if os.path.isfile(pdf_path):
        _ = publish_geometry_manifest(pdf_path, slug_dir, figures)
    else:
        log(f"  {slug}: geometry manifest skipped (source PDF missing)")
    log(f"  {slug}: {len(figures)} figures extracted")

    # Write review
    log(f"  {slug}: writing review...")
    if not write_review(
        item,
        slug_dir,
        figures,
        runtime_policy=runtime_policy,
    ):
        return "fail", "review_write_failed"
    review_path = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review_path) or os.path.getsize(review_path) < 200:
        return "fail", "review_write_failed"

    # Phase 3: tool-use schema enforces structured output, so the
    # post-hoc fixers (fix_python_list_literals / fix_evaluation_format /
    # fix_figure_paths / validate_review_format) are obsolete. Definitions
    # are kept below for backward-compat tooling but no longer invoked.

    # Convert to HTML
    convert_to_html(slug)
    html_path = os.path.join(slug_dir, "index.html")
    if not os.path.exists(html_path) or os.path.getsize(html_path) < 200:
        return "fail", "html_conversion_failed"

    return "ok", ""


def process_paper(item, slug, cp, runtime_policy=None):
    """Process a single paper with up to MAX_RETRIES auto-retries on failure."""
    if slug in cp["completed"]:
        return "skipped"

    slug_dir = os.path.join(PAPERS_DIR, slug)
    os.makedirs(slug_dir, exist_ok=True)

    # Clean existing files — but SKIP if paper belongs to other topics too
    if paper_has_other_topics(slug):
        log(f"  {slug}: belongs to multiple topics, skipping file deletion")
    else:
        for fname in ["text.md"]:
            fpath = os.path.join(slug_dir, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
        fig_dir = os.path.join(slug_dir, "figures")
        if os.path.isdir(fig_dir):
            shutil.rmtree(fig_dir)

    # Find PDF
    pdf_path, match_method = find_pdf(item)
    if not pdf_path:
        log(f"  {slug}: no PDF found (method={match_method})")
        with _cp_lock:
            cp["failed"].append({"slug": slug, "reason": f"no_pdf:{match_method}"})
        save_checkpoint(cp)
        return "no_pdf"
    # Record Zotero item key → slug mapping for index persistence (Phase 1
    # PDF integrity field). Keyed across threads via _cp_lock for safety.
    with _cp_lock:
        _slug_to_zotero_key[slug] = item.get("key", "")
        _slug_to_pdf_path[slug] = pdf_path
    if match_method == "fuzzy":
        log(f"  {slug}: PDF matched via FUZZY — review required")

    # Try up to MAX_RETRIES times
    last_reason = ""
    for attempt in range(1, MAX_RETRIES + 1):
        status, reason = _do_process(
            item,
            slug,
            slug_dir,
            pdf_path,
            runtime_policy,
        )
        if status == "ok":
            with _cp_lock:
                cp["completed"].append(slug)
            save_checkpoint(cp)
            if attempt > 1:
                log(f"  {slug}: succeeded on attempt {attempt}")
            return "ok"
        last_reason = reason
        log(f"  {slug}: attempt {attempt}/{MAX_RETRIES} failed ({reason})")
        if attempt < MAX_RETRIES:
            time.sleep(2)

    # All retries exhausted
    log(f"  {slug}: FAILED after {MAX_RETRIES} attempts ({last_reason})")
    with _cp_lock:
        cp["failed"].append({"slug": slug, "reason": last_reason})
    save_checkpoint(cp)
    return last_reason


# ── Main ──

# ── Phase 2: 3-axis mode mapping ─────────────────────────────────────────────
# MECE modes replace the 8-recipe legacy grid. Each --mode implies a specific
# combination of --resume/--skip-existing/--timeline/--category. When --mode
# is not used, the legacy flags behave exactly as before (backward compatible).
_MODE_LEGACY_MAP = {
    "curate":     {"resume": True,  "skip_existing": True,  "timeline": False, "category": False},
    "rebuild":    {"resume": False, "skip_existing": False, "timeline": False, "category": False},
    "reclassify": {"resume": True,  "skip_existing": True,  "timeline": False, "category": True},
    "retime":     {"resume": True,  "skip_existing": True,  "timeline": True,  "category": False},
}


def _apply_mode_mapping(args):
    """Translate --mode into legacy flag values.

    When args.mode is None (default), this is a no-op and existing callers keep
    working unchanged. When --mode is set, any conflicting legacy flag that was
    *explicitly* provided earns a DeprecationWarning, and the mode wins.
    """
    if args.mode is None:
        return  # legacy-only invocation, nothing to do

    target = _MODE_LEGACY_MAP[args.mode]
    # Detect explicit legacy flags that diverge from the mode's target
    overridden = []
    for field, expected in target.items():
        current = getattr(args, field)
        if current and not expected:
            overridden.append(f"--{field.replace('_', '-')}")
    if overridden:
        print(f"[deprecated] --mode {args.mode} overrides legacy flags: "
              f"{', '.join(overridden)} (these will be ignored)",
              file=sys.stderr)

    # Apply the mapping
    for field, value in target.items():
        setattr(args, field, value)
    print(f"[mode] {args.mode} → resume={args.resume}, skip_existing={args.skip_existing}, "
          f"timeline={args.timeline}, category={args.category}")


def _run_curate(topic, *, mode=None, concurrency=16, resume=False,
                 skip_existing=False, limit=0, timeline=False, category=False,
                 strict_pdf=False, slugs="", dry_run=False, skip_dedup=False,
                 dedup_execute=False):
    """Programmatic entrypoint. Patches ``sys.argv`` so the existing
    ``main()`` argparse parses the kwargs as if from the CLI. Behaviour-
    preserving; restores ``sys.argv`` on exit.
    """
    argv_backup = sys.argv[:]
    new_argv = ["run_update_force.py", "--topic", str(topic),
                "--concurrency", str(concurrency)]
    if mode:
        new_argv.extend(["--mode", str(mode)])
    if resume:
        new_argv.append("--resume")
    if skip_existing:
        new_argv.append("--skip-existing")
    if limit and limit > 0:
        new_argv.extend(["--limit", str(limit)])
    if timeline:
        new_argv.append("--timeline")
    if category:
        new_argv.append("--category")
    if strict_pdf:
        new_argv.append("--strict-pdf")
    if slugs:
        new_argv.extend(["--slugs", str(slugs)])
    if dry_run:
        new_argv.append("--dry-run")
    if skip_dedup:
        new_argv.append("--skip-dedup")
    if dedup_execute:
        new_argv.append("--dedup-execute")
    sys.argv = new_argv
    try:
        return main()
    finally:
        sys.argv = argv_backup


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-curation --update-force batch")
    parser.add_argument("--topic", default="ai4s", help="Topic: ai4s or scisci")
    parser.add_argument("--llm-mode", default=None)
    parser.add_argument("--concurrency", type=_positive_integer, default=1,
                        help="Serialized Codex worker count (safe profile requires 1)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip papers that already have review.md (for --update mode)")
    parser.add_argument("--limit", type=int, default=0, help="Limit papers (0=all)")
    parser.add_argument("--timeline", action="store_true",
                        help="Regenerate text timelines (with --resume: changed categories only)")
    parser.add_argument("--ensure-timeline", action="store_true",
                        help="기본 narrative/timeline 산출물이 없으면 생성하고, 업데이트 중 바뀐 카테고리만 보강한다.")
    parser.add_argument("--category", action="store_true",
                        help="Re-run topic_modeling to reclassify all papers. Auto-enables --timeline for changed categories.")
    parser.add_argument("--strict-pdf", action="store_true",
                        help="Disable fuzzy PDF matching — skip papers without Zotero/DOI/arXiv link.")
    parser.add_argument("--slugs", default="",
                        help="Comma-separated slug prefixes (e.g. '088,1093'). Only these papers are (re)processed. "
                             "Implies force-rebuild of the listed slugs regardless of checkpoint.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print which papers would be processed and exit without writing.")
    parser.add_argument("--skip-dedup", action="store_true",
                        help="Skip the Zotero duplicate preflight (saves ~1-5 min; not recommended).")
    parser.add_argument("--skip-metrics", action="store_true",
                        help="피인용수·레퍼런스 수집(citations.md/references.md) 스킵. "
                             "외부 API 를 타므로 오프라인이거나 급할 때.")
    parser.add_argument("--dedup-execute", action="store_true",
                        help="Let the Zotero dedup preflight actually delete duplicates. "
                             "Default is dry-run (report only).")
    parser.add_argument("--insights", action="store_true",
                        help="extract_insights 에서 cross-category insights(Option)까지 재생성. "
                             "기본은 paper connections(Core, '같이 보면 좋은 논문')만 생성 (--only connections).")
    parser.add_argument(
        "--local-fallback",
        action="store_true",
        help="Deprecated compatibility flag; no alternative model is invoked.",
    )
    parser.add_argument("--no-deploy", action="store_true",
                        help="Deprecated compatibility flag; deployment is removed in this fork.")
    parser.add_argument("--conn-full", action="store_true",
                        help="연결 캐시(_conn_topk_cache_k*.json)를 무시하고 이번 실행에서 전체 연결을 "
                             "재생성한다 (월간/대량 추가 후 주기적 full rebuild 용). 자식 프로세스"
                             "(extract_insights / topic_modeling)가 환경변수 CONN_FULL_REBUILD=1 로 "
                             "상속한다. 환경변수 CONN_FULL_REBUILD=1 로도 직접 켤 수 있다.")
    # ── Phase 2: 3-axis mode (new, MECE). When --mode is set, it overrides the
    # legacy flag combinations and emits DeprecationWarnings for any legacy
    # flags that were also specified. Omitting --mode keeps 100% legacy
    # behavior, so existing callers (incl. in-flight batches) are unaffected.
    parser.add_argument("--mode", choices=["curate", "rebuild", "reclassify", "retime"],
                        default="curate",
                        help="New MECE mode selector. curate=new papers only (keeps existing reviews); "
                             "rebuild=regenerate all reviews; reclassify=re-run topic modeling on existing reviews; "
                             "retime=regenerate timeline narratives+images only. "
                             "When set, overrides --resume/--skip-existing/--timeline/--category combinations.")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    # The release dry-run boundary is deliberately before policy/config,
    # interpreter probes, credentials, Zotero, checkpoints, leases, or writes.
    if args.dry_run:
        emit_release_result(
            build_dry_run_plan(
                entrypoint="pipeline/run_update_force.py",
                topic=args.topic,
                mode=args.mode,
                concurrency=args.concurrency,
                policy_mode=args.llm_mode or "codex",
            )
        )
        return 0

    try:
        preview_config: JsonObject = (
            {
                "runtime": {
                    "allow_paid_api": False,
                    "llm_mode": args.llm_mode,
                },
                "schema_version": 2,
            }
            if args.llm_mode is not None
            else cast(JsonObject, load_config())
        )
        preview_policy = resolve_runtime_policy(
            preview_config,
            args.llm_mode,
        )
    except RuntimePolicyError as error:
        print(f"Runtime policy denied: {error.code}", file=sys.stderr)
        return 2
    if preview_policy.mode == "off":
        emit_release_result(
            build_policy_denied_result(
                args.topic,
                Path(PAPERS_DIR).parent,
            )
        )
        return 3
    _apply_mode_mapping(args)
    runtime_policy = preview_policy
    safe_state_root = (
        args.state_dir.resolve()
        if args.state_dir is not None
        else PIPELINE_DIR / "_safe_update_state"
    )
    active_run = safe_state_root / "topics" / f"{args.topic}.active.json"
    resuming_safe_run = bool(args.resume and active_run.is_file())
    try:
        safe_run_lease = acquire_plan_lease(
            args.topic,
            preview_policy,
            state_root=safe_state_root,
            workspace_root=PROJECT_ROOT,
            resume=resuming_safe_run,
        )
    except (TopicBusyError, ResumeRequiredError) as error:
        log(f"SAFE RUN OWNERSHIP DENIED: {error.code}")
        return 75
    except Exception as error:
        log(f"SAFE RUN OWNERSHIP DENIED: {type(error).__name__}")
        return 2

    def close_safe_run(status: RunStatus) -> None:
        if not safe_run_lease.finished:
            safe_run_lease.finish(status)
        safe_run_lease.release()

    try:
        runtime_policy = _initialize_runtime(preview_policy)
        approved_python_path = approved_python()
    except Exception as error:
        log(f"SAFE RUN INITIALIZATION FAILED: {type(error).__name__}")
        close_safe_run(RunStatus.FAILED)
        return 2

    # Apply --mode → legacy flags mapping. Pure translation; no behavior change
    # when --mode is absent (args.mode is None → all legacy flags honored as-is).

    # --conn-full → force full connection regen for child subprocesses
    # (extract_insights / topic_modeling). They read CONN_FULL_REBUILD from env,
    # which is inherited across subprocess.run, so setting it here is enough.
    if getattr(args, "conn_full", False):
        os.environ["CONN_FULL_REBUILD"] = "1"
        print("[conn] --conn-full → CONN_FULL_REBUILD=1 "
              "(전체 연결 재생성; 자식 프로세스 env 상속)")

    # Propagate strict-pdf flag to find_pdf()
    global _STRICT_PDF
    _STRICT_PDF = args.strict_pdf
    if _STRICT_PDF:
        print("[strict-pdf] fuzzy PDF matching disabled — papers without authoritative PDF links will be skipped")

    # ── Rebuild safety: snapshot _papers_index.json so an accidental wipe
    # is reversible. Only triggered when --mode rebuild AND not --slugs (mass).
    if args.mode == "rebuild" and not args.slugs and not args.dry_run:
        idx_path = os.path.join(PAPERS_DIR, "_papers_index.json")
        if os.path.exists(idx_path):
            backup_path = os.path.join(PAPERS_DIR, "_papers_index.backup.json")
            try:
                shutil.copy2(idx_path, backup_path)
                print(f"[rebuild-safety] snapshot saved: {backup_path}")
            except Exception as _e:
                print(f"[rebuild-safety] snapshot FAILED: {_e}")

    # ── Preflight: Zotero dedup ────────────────────────────────────────────
    # Runs in ALL modes (full/local/update). Users may accidentally double-
    # register the same paper in Zotero via the UI or imports; running this
    # every time keeps the collection clean before we spend compute on reviews.
    # Default is dry-run → a report is written to {topic}/_dedup_zotero_report.json;
    # pass --dedup-execute once trust is built and you want auto-deletion.
    # `--slugs` and `--dry-run` modes skip the preflight because they are
    # targeted operations that should not touch the collection as a whole.
    if not args.skip_dedup and not args.slugs and not args.dry_run:
        dedup_script = os.path.join(os.path.dirname(__file__), "dedup_zotero.py")
        if os.path.exists(dedup_script):
            print(f"\n[preflight] Zotero dedup ({'EXECUTE' if args.dedup_execute else 'dry-run'}) for topic={args.topic}")
            dedup_cmd = [sys.executable, dedup_script, "--topic", args.topic]
            if args.dedup_execute:
                dedup_cmd.append("--execute")
            rc = subprocess.call(dedup_cmd)
            if rc != 0:
                print(f"[preflight] dedup exited {rc} — continuing anyway")

    collection_key = COLLECTIONS.get(args.topic, "")
    if not collection_key:
        print(f"Unknown topic: {args.topic}")
        close_safe_run(RunStatus.FAILED)
        return 2

    # Load checkpoint
    if args.resume:
        cp = load_checkpoint()
        # Clear failed list so they get retried
        prev_failed = len(cp.get("failed", []))
        cp["failed"] = []
        previously_completed = set(cp.get("completed", []))
        log(f"Resuming: {len(cp['completed'])} completed, {prev_failed} previously failed (will retry)")
    else:
        cp = {"completed": [], "failed": [], "phase": "init"}
        previously_completed = set()

    # Fetch items
    log(f"Fetching Zotero collection '{args.topic}' ({collection_key})...")
    items = fetch_zotero_items(collection_key)
    log(f"Total papers: {len(items)}")

    # Get existing slugs (create papers root on first run)
    os.makedirs(PAPERS_DIR, exist_ok=True)
    existing_slugs = sorted(d for d in os.listdir(PAPERS_DIR)
                            if os.path.isdir(os.path.join(PAPERS_DIR, d)) and d[0].isdigit())

    # Map items to slugs
    item_slug_pairs = []
    for item in items:
        slug = make_slug(item, existing_slugs)
        item_slug_pairs.append((item, slug))
        if slug not in existing_slugs:
            existing_slugs.append(slug)

    # --slugs filter: restrict processing to listed slug prefixes, and force-rebuild them
    if args.slugs:
        wanted_prefixes = [s.strip() for s in args.slugs.split(",") if s.strip()]
        before = len(item_slug_pairs)
        item_slug_pairs = [
            (it, slug) for (it, slug) in item_slug_pairs
            if any(slug.startswith(p) or slug == p for p in wanted_prefixes)
        ]
        log(f"--slugs: kept {len(item_slug_pairs)}/{before} papers matching {wanted_prefixes}")
        # Force rebuild: drop from checkpoint.completed so they are re-processed
        kept = {slug for _, slug in item_slug_pairs}
        cp["completed"] = [s for s in cp.get("completed", []) if s not in kept]
        save_checkpoint(cp)

    # Rebuild safety: preview PDF matching for first 5 papers so the
    # operator can spot fuzzy/wrong matches before the destructive run.
    if args.mode == "rebuild" and not args.slugs and not args.dry_run:
        print(f"\n[rebuild-preview] sampling 5 PDFs to verify matching before "
              f"processing {len(item_slug_pairs)} papers:")
        for it, slug in item_slug_pairs[:5]:
            try:
                _p, _m = find_pdf(it)
            except Exception as _e:
                _p, _m = None, f"err:{_e}"
            print(f"  {slug[:55]:55s} | method={_m} | path={_p or '(none)'}")
        print(f"[rebuild-preview] proceed if these look correct. "
              f"Ctrl-C now to abort.\n")

    # Skip papers with existing review.md (--skip-existing or --resume).
    # When --slugs is used the listed papers must be force-rebuilt, so they
    # are intentionally excluded from this "already done" shortcut.
    forced_slugs = set()
    if args.slugs:
        forced_slugs = {slug for _, slug in item_slug_pairs}

    # PDF-change auto-detect: if _papers_index.json has cached pdf_path for a
    # slug AND that PDF's mtime is newer than the slug's review.md, force a
    # rebuild. Cheap (stat-only) — no Zotero API calls. Cache populated by
    # prior find_pdf hits and persisted in the index.
    if args.skip_existing or args.resume:
        try:
            _idx_path = os.path.join(PAPERS_DIR, "_papers_index.json")
            if os.path.exists(_idx_path):
                with open(_idx_path, "r", encoding="utf-8") as _f:
                    _cached = {p["slug"]: p.get("pdf_path", "") for p in json.load(_f)}
                _pdf_detected = []
                for _, slug in item_slug_pairs:
                    if slug in forced_slugs:
                        continue
                    _pdf = _cached.get(slug, "")
                    _review = os.path.join(PAPERS_DIR, slug, "review.md")
                    if _pdf and os.path.exists(_pdf) and os.path.exists(_review):
                        if os.path.getmtime(_pdf) > os.path.getmtime(_review):
                            _pdf_detected.append(slug)
                if _pdf_detected:
                    forced_slugs.update(_pdf_detected)
                    log(f"[pdf-change] {len(_pdf_detected)} papers: PDF mtime > review.md mtime — forcing rebuild")
                    for _s in _pdf_detected[:10]:
                        log(f"  - {_s}")
                    if len(_pdf_detected) > 10:
                        log(f"  ... +{len(_pdf_detected)-10} more")
        except Exception as _e:
            log(f"[pdf-change] check skipped: {_e}")

    if forced_slugs:
        cp["completed"] = [
            slug
            for slug in cp.get("completed", [])
            if slug not in forced_slugs
        ]
        save_checkpoint(cp)

    if args.skip_existing or args.resume:
        skipped = 0
        for item, slug in item_slug_pairs:
            if slug in forced_slugs:
                continue
            review_path = os.path.join(PAPERS_DIR, slug, "review.md")
            if os.path.exists(review_path) and os.path.getsize(review_path) >= 200:
                if slug not in cp["completed"]:
                    cp["completed"].append(slug)
                    skipped += 1
        save_checkpoint(cp)
        log(f"--skip-existing: {skipped} papers with existing review.md marked as completed")

    # Filter already completed
    remaining = [(item, slug) for item, slug in item_slug_pairs
                 if slug not in cp["completed"]]

    if args.limit > 0:
        remaining = remaining[:args.limit]

    log(f"To process: {len(remaining)} (completed: {len(cp['completed'])}, failed: {len(cp['failed'])})")
    log(f"Concurrency: {args.concurrency}")
    log(f"Estimated time: ~{len(remaining) * 5 / args.concurrency / 60:.1f} hours")

    # Process with thread pool while the topic lease is already held.
    start_time = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {}
        for item, slug in remaining:
            future = executor.submit(
                process_paper,
                item,
                slug,
                cp,
                runtime_policy,
            )
            futures[future] = slug

        for future in as_completed(futures):
            slug = futures[future]
            try:
                result = future.result()
                done += 1
                elapsed = time.time() - start_time
                rate = elapsed / done if done else 0
                eta = rate * (len(remaining) - done) / 3600
                log(f"[{done}/{len(remaining)}] {slug}: {result} (ETA: {eta:.1f}h)")
            except Exception as e:
                log(f"[{done}/{len(remaining)}] {slug}: ERROR {e}")
                with _cp_lock:
                    cp["failed"].append({"slug": slug, "reason": str(e)})
                save_checkpoint(cp)

    elapsed_total = (time.time() - start_time) / 3600
    log(f"\nPass 1 done! {done} papers in {elapsed_total:.1f}h")
    log(f"Completed: {len(cp['completed'])}, Failed: {len(cp['failed'])}")

    # Note: per-paper auto-retry (MAX_RETRIES=3) is built into process_paper.
    # No separate retry pass needed.

    log(f"\nFinal: {len(cp['completed'])} completed, {len(cp['failed'])} failed")

    # ── Persist Zotero item key + PDF path → slug mapping into _papers_index.json ──
    # build_papers_index will preserve `zotero_item_key` and `pdf_path` via prev.get(...).
    if _slug_to_zotero_key or _slug_to_pdf_path:
        try:
            from pipeline.lib.atomic_io import atomic_write_json
            idx_path = os.path.join(PAPERS_DIR, "_papers_index.json")
            if os.path.exists(idx_path):
                with open(idx_path, "r", encoding="utf-8") as f:
                    _idx = json.load(f)
                _patched_key = 0
                _patched_pdf = 0
                for _e in _idx:
                    _s = _e.get("slug")
                    _k = _slug_to_zotero_key.get(_s)
                    if _k and _e.get("zotero_item_key") != _k:
                        _e["zotero_item_key"] = _k
                        _patched_key += 1
                    _p = _slug_to_pdf_path.get(_s)
                    if _p and _e.get("pdf_path") != _p:
                        _e["pdf_path"] = _p
                        _patched_pdf += 1
                if _patched_key or _patched_pdf:
                    atomic_write_json(idx_path, _idx)
                    log(f"  [persist] zotero_item_key={_patched_key}, pdf_path={_patched_pdf} mappings into _papers_index.json")
        except Exception as _e:
            log(f"  [persist] failed: {_e}")

    # ── Post-processing: rebuild index, classify, insights, topic page ──
    topic = args.topic
    newly_completed = {slug for _item, slug in remaining if slug in cp.get("completed", [])}
    do_timeline_text = args.timeline or args.category
    do_ensure_timeline = getattr(args, "ensure_timeline", False)
    missing_default_artifacts = (
        _topic_default_artifacts_missing(topic)
        if (do_ensure_timeline or do_timeline_text)
        else []
    )
    base_needs_postprocess = (
        bool(newly_completed)
        or (len(cp["completed"]) > 0 and not args.resume)
        or bool(args.timeline)
        or bool(args.category)
        or bool(missing_default_artifacts)
    )
    needs_postprocess = base_needs_postprocess or resuming_safe_run
    if not needs_postprocess:
        log("\nPost-processing skipped: no new papers, no explicit reclassify/retime, "
            "and default timeline artifacts already exist.")
    if needs_postprocess:
        log("\n" + "=" * 60)
        log("SAFE POST-PROCESSING: retained local stages")
        log("=" * 60)

        all_slugs = sorted(set(cp.get("completed", [])))
        validated_review_slugs = [
            slug
            for slug in all_slugs
            if (
                (Path(PAPERS_DIR) / slug / "review.md").is_file()
                and (
                    Path(PAPERS_DIR)
                    / slug
                    / "figures"
                    / "manifest-v1.json"
                ).is_file()
            )
        ]
        planned = default_stage_plan(
            topic,
            runtime_policy,
            executable=approved_python_path,
            paper_slugs=all_slugs,
        )
        runnable = []
        for stage in planned:
            if stage.name in {"review", "geometry"}:
                if not validated_review_slugs:
                    continue
                if stage.name == "review":
                    outputs = tuple(
                        f"docs/papers/{slug}/review.md"
                        for slug in validated_review_slugs
                    )
                    names = tuple("review" for _ in outputs)
                else:
                    outputs = tuple(
                        f"docs/papers/{slug}/figures/manifest-v1.json"
                        for slug in validated_review_slugs
                    )
                    names = tuple("geometry" for _ in outputs)
                runnable.append(Stage(stage.name, (), outputs, names))
                continue
            runnable.append(stage)

        timeouts = {
            "classification": 3600,
            "summary": 1200,
            "connection": 14400,
            "timeline": 21600,
        }

        def run_safe_stage(stage):
            if not stage.argv:
                if resuming_safe_run and stage.name in {"review", "geometry"}:
                    missing = [
                        output
                        for output in stage.outputs
                        if not (Path(PROJECT_ROOT) / output).is_file()
                    ]
                    if missing:
                        raise RuntimeError(
                            f"in-process artifact changed before resume: "
                            f"{stage.name} (missing {missing[0]})"
                        )
                    log(f"  [{stage.name}] REUSED: validated in-process artifact")
                    return
                log(f"  [{stage.name}] REUSED: validated in-process artifact")
                return
            if stage.name == "bm25":
                _ = build_transitional_sparse_index(topic, PROJECT_ROOT)
                log("  [bm25] OK: deterministic sparse v2 sidecar")
                return
            log(f"  [{stage.name}] ...")
            result = subprocess.run(
                list(stage.argv),
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=timeouts.get(stage.name, 600),
                env=safe_child_environment(runtime_policy),
            )
            if result.returncode != 0:
                diagnostic = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"safe stage '{stage.name}' failed "
                    f"(exit {result.returncode}): {diagnostic[-1000:]}"
                )
            log(f"  [{stage.name}] OK")

        try:
            _ = execute_plan(
                tuple(runnable),
                topic=topic,
                policy=runtime_policy,
                state_root=safe_state_root,
                workspace_root=PROJECT_ROOT,
                runner=run_safe_stage,
                resume=resuming_safe_run,
                lease=safe_run_lease,
                finalize=False,
            )
        except Exception as error:
            log(f"SAFE POST-PROCESSING FAILED: {error}")
            return 2

        from pipeline.refresh_retrieval_eval_snapshot import (
            main as refresh_retrieval_snapshot,
        )

        refresh_exit = refresh_retrieval_snapshot(
            [
                "--project-root",
                str(PROJECT_ROOT),
                "--if-installed",
            ]
        )
        if refresh_exit != 0:
            log("SAFE POST-PROCESSING FAILED: retrieval snapshot refresh")
            close_safe_run(RunStatus.FAILED)
            return 2

    try:
        accepted_artifacts = validate_default_artifacts(
            topic,
            Path(PAPERS_DIR).parent,
        )
        geometry_paths = [
            f"docs/{artifact['path']}"
            for row in accepted_artifacts
            if row["stage"] == "geometry"
            for artifact in cast(list[dict[str, str]], row["artifacts"])
        ]
        _ = artifact_manifest(
            topic,
            runtime_policy,
            PROJECT_ROOT,
            geometry_paths=geometry_paths,
            validators=accepted_artifacts,
        )
    except (ArtifactValidationError, OSError, ValueError) as error:
        log(f"SAFE ARTIFACT ACCEPTANCE FAILED: {error}")
        close_safe_run(RunStatus.FAILED)
        return 2

    close_safe_run(RunStatus.SUCCEEDED)
    log("\n  [prepare_deploy] SKIP: deployment is outside the local-safe profile")
    log("\nPost-processing complete!")
    return 0


if __name__ == "__main__":
    from pipeline._env_guard import force_py312
    force_py312()
    raise SystemExit(main())
