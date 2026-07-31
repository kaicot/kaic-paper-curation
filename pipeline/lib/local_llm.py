"""Local LLM fallback client (OpenAI-compatible endpoint).

한국망↔Anthropic 이 끝까지 막혀 ``topic_modeling.generate_connections_from_candidates``
가 max_rounds 를 다 돌고도 *연결을 못 만든 papers* 가 남을 때, **로컬에서 도는
모델로 그 잔여분을 마저 연결**하는 opt-in fallback 의 클라이언트.

클라우드 키·외부 네트워크 없이 동작하며, 환경에 로컬 모델이 떠 있고 사용자가
``--local-fallback`` 을 켰을 때만 실행된다. OpenAI 호환 엔드포인트(/v1/chat/completions
+ /v1/models) 만 가정하므로 러너에 독립적이다:

  - Ollama:    base_url=http://localhost:11434/v1   model=qwen2.5:7b-instruct
  - LM Studio: base_url=http://localhost:1234/v1    model=<loaded model id>
  - llama.cpp: base_url=http://localhost:8080/v1    model=<any>
  - vLLM:      base_url=http://localhost:8000/v1    model=<served name>

설정은 config.json 의 ``local_model`` 블록 또는 환경변수
(LOCAL_MODEL_BASE_URL / LOCAL_MODEL_NAME / LOCAL_MODEL_API_KEY) 로 준다
(config_loader.get_local_model_config 참조).
"""
import json
import re
import urllib.error
import urllib.request

# 가장 바깥 JSON object 추출용 (로컬 모델은 서론/코드펜스/trailing comma 를 자주 붙인다)
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# thinking 모델(EXAONE-4.5, Qwen3 등)의 reasoning trace. JSON 추출 전에 반드시
# 제거 — trace 안에 중괄호가 섞이면 바깥 {...} 추출이 잘못된 범위를 잡는다.
# 닫는 태그가 잘린 경우(<think>만 있고 max_tokens 절단)도 방어한다.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN = re.compile(r"<think>.*", re.DOTALL)


def probe(base_url, timeout=5.0):
    """엔드포인트가 살아 있는지 GET {base_url}/models 로 빠르게 확인. 실패해도 예외 없이 False."""
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer local"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:
        return False


def _ollama_root(base_url):
    """OpenAI 호환 base_url(.../v1) → Ollama 서버 루트."""
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def detect_ollama(base_url, timeout=5.0):
    """base_url 이 Ollama 서버인지 GET /api/version 으로 감지. 실패하면 False.

    Ollama 면 네이티브 /api/chat 전송을 쓴다 — OpenAI 호환 endpoint 가 못 하는
    것들(요청 단위 num_ctx, 정식 think:false)이 네이티브에는 있다.
    """
    try:
        req = urllib.request.Request(_ollama_root(base_url) + "/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.load(r).get("version"))
    except Exception:
        return False


def get_client(cfg):
    """OpenAI SDK 를 로컬 base_url 로 향하게 한 클라이언트. SDK 미설치/실패 시 None."""
    try:
        from openai import OpenAI
    except Exception:
        return None
    try:
        return OpenAI(
            base_url=cfg["base_url"],
            api_key=cfg.get("api_key") or "local",
            timeout=float(cfg.get("timeout", 300.0)),
            max_retries=1,
        )
    except Exception:
        return None


def chat_json(client, model, prompt, max_tokens=8000, temperature=0.2,
              reasoning_effort=None, json_mode=False):
    """단일 chat completion → 첫 JSON object 를 dict 로 견고하게 파싱.

    로컬 모델은 ```json 펜스·서론 문장·trailing comma 를 흘리곤 한다. 방어:
      1) ```...``` 코드펜스 벗기기  2) 바깥 {...} 추출  3) trailing comma 제거
    파싱 실패는 호출자가 잡도록 그대로 예외를 올린다(해당 배치만 건너뜀).

    reasoning_effort: thinking 모델(EXAONE-4.5 등) 제어. Ollama 의 OpenAI 호환
    endpoint 는 요청 body 의 "think" 필드를 무시하지만 "reasoning_effort" 는
    매핑한다 — "none" 이면 think OFF (실측 2026-06-12: EXAONE-4.5 가
    reasoning_effort 없이는 content 가 빈 채 thinking 채널에 토큰을 다 쓰고,
    "none" 이면 0.8s 에 깨끗한 JSON 을 낸다). config.json local_model 블록의
    "reasoning_effort" 키로 지정.

    json_mode: True 면 response_format={"type":"json_object"} 로 문법 제약
    디코딩 — 서버가 JSON 유효성을 구조적으로 보장한다 (Ollama/llama.cpp 의
    GBNF grammar). 자유 생성은 같은 모델이라도 배치에 따라 형식이 깨질 수
    있어(실측: EXAONE-4.5 no-think 2회 중 1회 delimiter 오류) Ollama 류
    서버에서는 켜는 것을 권장. response_format 미지원 서버면 빼면 된다.
    """
    extra = {}
    if reasoning_effort:
        extra["extra_body"] = {"reasoning_effort": reasoning_effort}
    if json_mode:
        extra["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )
    return _parse_json_text(resp.choices[0].message.content or "")


def _parse_json_text(text):
    """모델 출력 → dict. think trace·코드펜스·서론·trailing comma 방어 공유 로직."""
    text = text.strip()
    # thinking 모델 방어: reasoning trace 를 JSON 추출 전에 제거.
    # 완결 블록 우선, 닫는 태그 없이 끝나면(절단) <think> 이후 전부 버린다.
    text = _THINK_BLOCK.sub("", text)
    if "<think>" in text:
        text = _THINK_OPEN.sub("", text)
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    m = _JSON_OBJ.search(text)
    if m:
        text = m.group(0)
    text = re.sub(r",\s*([}\]])", r"\1", text)  # trailing comma 정리
    return json.loads(text)


def chat_json_native(base_url, model, prompt, max_tokens=8000, temperature=0.2,
                     num_ctx=8192, timeout=600.0):
    """Ollama 네이티브 /api/chat 전송 → dict (detect_ollama 가 True 일 때 사용).

    OpenAI 호환 endpoint 대신 네이티브를 쓰는 이유 (실측 2026-06-12):
      - options.num_ctx: 신형 Ollama 는 모델의 최대 컨텍스트(128K~256K)를 기본
        로드해 KV 캐시가 비대해진다. 연결 배치는 ~1.2K 토큰이라 8K 면 충분 —
        요청 단위로 줄여 전역 설정을 건드리지 않고 속도/메모리를 확보한다.
      - think:false 정식 지원: thinking 모델(EXAONE-4.5 등)이 content 를 비우는
        문제를 우회 없이 끈다 (OpenAI 호환은 reasoning_effort 매핑에 의존).
    format(grammar) 은 신형 엔진 아키텍처에서 무시되는 경우가 있어 신뢰하지
    않는다 — 유효성은 _parse_json_text + 호출자의 배치 재시도가 책임진다.
    thinking 미지원 모델이 "think" 파라미터를 400 으로 거부하면 빼고 1회 재시도.
    """
    url = _ollama_root(base_url) + "/api/chat"
    payload = {
        "model": model, "stream": False, "think": False,
        "options": {"num_predict": max_tokens, "temperature": temperature,
                    "num_ctx": int(num_ctx)},
        "messages": [{"role": "user", "content": prompt}],
    }
    for attempt in (0, 1):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            # 일부 모델은 thinking 미지원이라 "think" 자체를 400 으로 거부
            if attempt == 0 and e.code == 400 and "think" in detail.lower():
                payload.pop("think", None)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail[:160]}") from e
    text = (resp.get("message", {}) or {}).get("content") or ""
    return _parse_json_text(text)
