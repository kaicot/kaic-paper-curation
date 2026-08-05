"""
SPECTER2 임베딩 공유 로더 — AI2 가 의도한 방식 (base encoder + PROXIMITY adapter + [CLS] pooling).

기존 topic_modeling.py 는 `SentenceTransformer(specter2_base)` 로 base 인코더만
로드해 mean pooling 을 썼다 (로그: "No sentence-transformers model found ...
mean pooling"). 하지만 AI2 가 설계한 SPECTER2 는 base BERT 위에 proximity
adapter 를 얹고 [CLS] 토큰을 문서 임베딩으로 사용한다. 이 모듈은 그 정식 경로를
구현한다. proximity adapter runtime이나 cache가 없으면 명시적으로 unavailable이다.

로컬 캐시 전용: 정상 실행은 프로젝트 `.cache/` 의 `base/` +
`adapters/proximity/` 만 읽는다. 캐시가 없으면 명시적으로 실패하며 원격 다운로드를
시도하지 않는다. 다운로드는 `prepare_local_models.py --specter2` 에서만 허용한다.

EMBED_TAG 로 어떤 경로로 임베딩했는지 표시한다:
  - "specter2-proximity-cls-v1:<manifest_sha256>"

caller (topic_modeling / classify_papers) 는 이 태그를 임베딩 캐시 JSON 과 joblib
번들에 박아 넣어, 모델이 바뀌었을 때 구·신 벡터가 섞이는 silent corruption 을
막는다 (태그가 다르면 캐시 무효화 / 번들 분류 거부).
"""

from pathlib import Path

import numpy as np
from .specter2_cache import (
    PROFILE,
    Specter2CacheUnavailable,
    embedding_identity,
    verify_cache,
)

# ── 경로 ──────────────────────────────────────────
# pipeline/lib/specter2_embed.py → parent(lib) → parent(pipeline) → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_ROOT = _PROJECT_ROOT / ".cache"
_BASE_LOCAL = _CACHE_ROOT / "base"
_PROX_LOCAL = _CACHE_ROOT / "adapters" / "proximity"
_BASE_REF = str(_BASE_LOCAL)
_PROX_REF = str(_PROX_LOCAL)

try:
    EMBED_TAG = embedding_identity(_CACHE_ROOT, verify_files=False)
except Specter2CacheUnavailable:
    EMBED_TAG = f"{PROFILE}:unavailable"

# 싱글톤 상태: 모델/토크나이저/태그를 1회만 로드한다.
_STATE = {
    "loaded": False,
    "tag": None,
    "model": None,      # adapter 모드: AutoAdapterModel
    "tokenizer": None,  # adapter 모드: AutoTokenizer
    "device": None,     # adapter 모드: "mps" or "cpu"
}


Specter2Unavailable = Specter2CacheUnavailable


def local_cache_status(cache_root=None):
    """Return local-cache availability without importing ML libraries."""
    root = Path(cache_root) if cache_root is not None else _CACHE_ROOT
    try:
        manifest = verify_cache(root)
    except Specter2CacheUnavailable as error:
        return {"available": False, "reason": str(error)}
    return {"available": True, "manifest_sha256": manifest["manifest_sha256"]}


def _log(msg):
    print(f"[specter2_embed] {msg}", flush=True)


def load_specter2():
    """SPECTER2 (base + proximity adapter, [CLS] pooling) 를 로드.

    싱글톤 — 두 번째 호출부터는 캐시된 상태를 그대로 돌려준다.

    Returns: 상태 dict. 주요 키:
      - "tag": manifest-bound embedding identity
      - "model", "tokenizer", "device"
    """
    global EMBED_TAG
    if _STATE["loaded"]:
        return _STATE
    try:
        identity = embedding_identity(_CACHE_ROOT)
    except Specter2CacheUnavailable as error:
        raise Specter2Unavailable(
            "specter2-cache-unavailable: run pipeline/prepare_local_models.py --specter2"
        ) from error

    try:
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer
        import torch

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _log(f"loading base encoder: {_BASE_REF}")
        tokenizer = AutoTokenizer.from_pretrained(_BASE_REF, local_files_only=True)
        model = AutoAdapterModel.from_pretrained(_BASE_REF, local_files_only=True)

        _log(f"loading proximity adapter (local): {_PROX_REF}")
        adapter_name = model.load_adapter(_PROX_REF, set_active=True)
        # belt-and-suspenders: set_active=True 가 이미 활성화하지만 명시적으로
        # 한 번 더 고정해 "none activated for forward pass" 경고를 차단한다.
        model.set_active_adapters(adapter_name)
        model = model.to(device).eval()

        _STATE.update({
            "loaded": True, "tag": identity,
            "model": model, "tokenizer": tokenizer, "device": device,
        })
        EMBED_TAG = identity
        _log(f"ready — ADAPTER mode (proximity '{adapter_name}', [CLS] pooling, "
             f"device={device}, tag={identity})")
        return _STATE

    except ImportError as e:
        raise Specter2Unavailable(
            "specter2 runtime dependencies are unavailable"
        ) from e


def embed_texts(texts, batch_size=8):
    """텍스트 리스트 → SPECTER2 임베딩 (np.ndarray float32, shape=(N, 768)).

    tokenize(truncation, max_length=512, padding) → forward →
    last_hidden_state[:, 0, :] ([CLS]) 를 문서 임베딩으로 사용. torch.no_grad.
    """
    texts = list(texts)
    state = load_specter2()

    if not texts:
        return np.zeros((0, 768), dtype=np.float32)

    import torch

    model = state["model"]
    tokenizer = state["tokenizer"]
    device = state["device"]

    chunks = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = [t if (t and t.strip()) else " " for t in texts[i:i + batch_size]]
            inputs = tokenizer(
                batch, truncation=True, max_length=512,
                padding=True, return_tensors="pt",
            ).to(device)
            out = model(**inputs)
            last_hidden = out.last_hidden_state if hasattr(out, "last_hidden_state") \
                else out[0]
            cls = last_hidden[:, 0, :].cpu().numpy().astype(np.float32)
            chunks.append(cls)
    return np.vstack(chunks).astype(np.float32)
