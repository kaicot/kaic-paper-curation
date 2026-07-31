"""
SPECTER2 임베딩 공유 로더 — AI2 가 의도한 방식 (base encoder + PROXIMITY adapter + [CLS] pooling).

기존 topic_modeling.py 는 `SentenceTransformer(specter2_base)` 로 base 인코더만
로드해 mean pooling 을 썼다 (로그: "No sentence-transformers model found ...
mean pooling"). 하지만 AI2 가 설계한 SPECTER2 는 base BERT 위에 proximity
adapter 를 얹고 [CLS] 토큰을 문서 임베딩으로 사용한다. 이 모듈은 그 정식 경로를
구현하고, `adapters` 라이브러리가 없으면 기존 SentenceTransformer/mean-pooling 으로
graceful fallback 한다.

로컬 캐시 우선: 한국 망에서 huggingface.co LFS 가 막히므로 프로젝트 `.cache/` 의
`base/` + `adapters/proximity/` 가 있으면 네트워크 없이 로드한다 (검증됨).

EMBED_TAG 로 어떤 경로로 임베딩했는지 표시한다:
  - "specter2_proximity_cls"  : adapter 모드 (정상 — AI2 권장)
  - "specter2_base_mean"      : fallback (adapters 미설치)

caller (topic_modeling / classify_papers) 는 이 태그를 임베딩 캐시 JSON 과 joblib
번들에 박아 넣어, 모델이 바뀌었을 때 구·신 벡터가 섞이는 silent corruption 을
막는다 (태그가 다르면 캐시 무효화 / 번들 분류 거부).
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np

# ── 경로 ──────────────────────────────────────────
# pipeline/lib/specter2_embed.py → parent(lib) → parent(pipeline) → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BASE_LOCAL = _PROJECT_ROOT / ".cache" / "base"
_PROX_LOCAL = _PROJECT_ROOT / ".cache" / "adapters" / "proximity"

# 로컬 캐시가 있으면 거기서, 없으면 HF Hub 이름 fallback.
_BASE_REF = str(_BASE_LOCAL) if (_BASE_LOCAL / "config.json").exists() \
            else "allenai/specter2_base"
_PROX_IS_LOCAL = (_PROX_LOCAL / "adapter_config.json").exists()
_PROX_REF = str(_PROX_LOCAL) if _PROX_IS_LOCAL else "allenai/specter2"

# ── 태그 ──────────────────────────────────────────
TAG_ADAPTER = "specter2_proximity_cls"
TAG_FALLBACK = "specter2_base_mean"


def _resolve_tag():
    """현재 환경에서 embed_texts 가 사용할 모드 태그를 결정.

    `adapters` 모듈을 import 하지 않고 find_spec 으로 존재만 확인 (torch 로딩
    오버헤드 회피). 실제 로드는 진짜 import 를 try/except 로 감싸므로, find_spec
    이 거짓 양성을 내도 load_specter2 가 loud fallback 으로 EMBED_TAG 를
    정정한다.
    """
    return TAG_ADAPTER if importlib.util.find_spec("adapters") is not None \
        else TAG_FALLBACK


# 모듈 import 시 1회 결정 (find_spec 만 — 가볍다). 로드가 fallback 하면
# load_specter2 가 이 전역을 정정한다.
EMBED_TAG = _resolve_tag()

# 싱글톤 상태: 모델/토크나이저/태그를 1회만 로드한다.
_STATE = {
    "loaded": False,
    "tag": None,
    "model": None,      # adapter 모드: AutoAdapterModel
    "tokenizer": None,  # adapter 모드: AutoTokenizer
    "device": None,     # adapter 모드: "mps" or "cpu"
    "st_model": None,   # fallback 모드: SentenceTransformer
}


def _warn(msg):
    """fallback 등 운영자가 반드시 봐야 하는 경고를 stderr 로 크게 출력."""
    bar = "!" * 72
    print(f"\n{bar}\n[specter2_embed] {msg}\n{bar}", file=sys.stderr, flush=True)


def _log(msg):
    print(f"[specter2_embed] {msg}", flush=True)


def load_specter2():
    """SPECTER2 (base + proximity adapter, [CLS] pooling) 를 로드.

    싱글톤 — 두 번째 호출부터는 캐시된 상태를 그대로 돌려준다.

    Returns: 상태 dict. 주요 키:
      - "tag"   : TAG_ADAPTER ("specter2_proximity_cls") 또는
                  TAG_FALLBACK ("specter2_base_mean")
      - adapter 모드: "model", "tokenizer", "device"
      - fallback 모드: "st_model"
    """
    global EMBED_TAG
    if _STATE["loaded"]:
        return _STATE

    try:
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer
        import torch

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _log(f"loading base encoder: {_BASE_REF}")
        tokenizer = AutoTokenizer.from_pretrained(_BASE_REF)
        model = AutoAdapterModel.from_pretrained(_BASE_REF)

        # proximity adapter 활성화. 로컬 경로면 source 지정 불필요, HF 이름이면
        # source="hf" 로 Hub 에서 받는다 (allenai/specter2 의 proximity adapter).
        if _PROX_IS_LOCAL:
            _log(f"loading proximity adapter (local): {_PROX_REF}")
            adapter_name = model.load_adapter(_PROX_REF, set_active=True)
        else:
            _log(f"loading proximity adapter (hf): {_PROX_REF}")
            adapter_name = model.load_adapter(_PROX_REF, source="hf", set_active=True)
        # belt-and-suspenders: set_active=True 가 이미 활성화하지만 명시적으로
        # 한 번 더 고정해 "none activated for forward pass" 경고를 차단한다.
        model.set_active_adapters(adapter_name)
        model = model.to(device).eval()

        _STATE.update({
            "loaded": True, "tag": TAG_ADAPTER,
            "model": model, "tokenizer": tokenizer, "device": device,
        })
        EMBED_TAG = TAG_ADAPTER
        _log(f"ready — ADAPTER mode (proximity '{adapter_name}', [CLS] pooling, "
             f"device={device}, tag={TAG_ADAPTER})")
        return _STATE

    except ImportError as e:
        _warn(
            "proximity adapter unavailable — falling back to bare "
            "specter2_base/mean-pooling. Clustering quality will be lower. "
            f"Install with: pip install adapters  (import error: {e})"
        )
        from sentence_transformers import SentenceTransformer
        st = SentenceTransformer(_BASE_REF, local_files_only=True)
        _STATE.update({
            "loaded": True, "tag": TAG_FALLBACK, "st_model": st,
        })
        EMBED_TAG = TAG_FALLBACK
        _log(f"ready — FALLBACK mode (base/mean-pooling, tag={TAG_FALLBACK})")
        return _STATE


def embed_texts(texts, batch_size=8):
    """텍스트 리스트 → SPECTER2 임베딩 (np.ndarray float32, shape=(N, 768)).

    adapter 모드: tokenize(truncation, max_length=512, padding) → forward →
    last_hidden_state[:, 0, :] ([CLS]) 를 문서 임베딩으로 사용. torch.no_grad.
    fallback 모드: SentenceTransformer.encode (mean pooling).
    """
    texts = list(texts)
    state = load_specter2()

    if not texts:
        return np.zeros((0, 768), dtype=np.float32)

    if state["tag"] == TAG_ADAPTER:
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

    # fallback: SentenceTransformer (mean pooling). batch_size 가 너무 작으면
    # ST 처리량이 떨어지므로 최소 32 로 끌어올린다 (기존 동작과 동일).
    st = state["st_model"]
    emb = st.encode(texts, batch_size=max(batch_size, 32), show_progress_bar=False)
    return np.asarray(emb, dtype=np.float32)
