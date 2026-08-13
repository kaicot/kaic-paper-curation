"""
config.json 로더 + Zotero User ID / Collection Key 자동 조회.

모든 스크립트가 이 모듈을 통해 설정을 읽는다.
config.json이 없으면 환경변수 폴백.

Collection은 이름(예: "AI assisted Research")으로 지정하면
Zotero API로 collection key를 자동 조회한다.
"""

import json
import os
import ssl
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

if __package__:
    from .runtime_policy import JsonObject, resolve_runtime_policy
else:
    from runtime_policy import JsonObject, resolve_runtime_policy

_ssl_ctx = ssl.create_default_context()

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 배포 파일 경로 (GitHub Pages 서빙 루트)
DOCS_DIR = PROJECT_ROOT / "docs"
PAPERS_DIR = DOCS_DIR / "papers"

# 타임라인/워크플로우 이미지 출력
IMG_TIMELINES_DIR = PIPELINE_DIR / "_img_timelines"
IMG_WORKFLOWS_DIR = PIPELINE_DIR / "_img_workflows"

REPO = PROJECT_ROOT  # backward compat alias

_config_cache = None
_user_id_cache = None
_collection_key_cache = None


def _windows_known_profile() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = cast(
        int,
        ctypes.windll.shell32.SHGetFolderPathW(
            None,
            40,
            None,
            0,
            buffer,
        ),
    )
    value = cast(str, buffer.value)
    if result != 0 or not value:
        raise ValueError("profile-unavailable")
    return Path(value)


def resolve_user_profile(
    environment: Mapping[str, str] | None = None,
    *,
    known_folder: Callable[[], Path] | None = None,
) -> Path:
    """Resolve one explicit profile boundary without calling Path.home()."""
    source = os.environ if environment is None else environment
    declared = source.get("USERPROFILE", "").strip()
    home = source.get("HOME", "").strip()
    drive = source.get("HOMEDRIVE", "").strip()
    tail = source.get("HOMEPATH", "").strip()
    drive_home = drive + tail if drive and tail else ""
    values = [value for value in (declared, home, drive_home) if value]
    if values:
        declared_paths = [Path(value) for value in values]
        for path in declared_paths:
            if (
                not path.is_absolute()
                or path.is_symlink()
                or path.is_junction()
            ):
                raise ValueError("profile-invalid")
        resolved = [path.resolve() for path in declared_paths]
        normalized = {os.path.normcase(str(value)) for value in resolved}
        if len(normalized) != 1:
            raise ValueError("profile-mismatch")
        profile = resolved[0]
    else:
        resolver = known_folder or _windows_known_profile
        profile = resolver().resolve()
    if not profile.is_absolute() or profile.is_symlink():
        raise ValueError("profile-invalid")
    return profile


def local_zotero_status(
    config: Mapping[str, object] | None = None,
) -> dict[str, bool | int]:
    """Return value-free local Zotero readiness without API access."""
    source = cast(
        Mapping[str, object],
        load_config() if config is None else config,
    )
    raw_zotero = source.get("zotero", {})
    zotero: Mapping[str, object]
    if isinstance(raw_zotero, dict):
        zotero = cast(dict[str, object], raw_zotero)
    else:
        zotero = {}
    raw_collections = zotero.get("collections", {})
    collections: Mapping[str, object]
    if isinstance(raw_collections, dict):
        collections = cast(dict[str, object], raw_collections)
    else:
        collections = {}
    raw_pdf_dir = zotero.get("pdf_dir", "")
    pdf_dir = raw_pdf_dir.strip() if isinstance(raw_pdf_dir, str) else ""

    def configured(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    return {
        "api_key_configured": configured(zotero.get("api_key")),
        "collection_count": sum(
            1
            for topic, value in collections.items()
            if configured(topic) and configured(value)
        ),
        "email_configured": configured(
            zotero.get("email", source.get("unpaywall_email"))
        ),
        "pdf_dir_configured": bool(pdf_dir),
        "pdf_dir_exists": bool(
            pdf_dir
            and Path(pdf_dir).is_dir()
            and not Path(pdf_dir).is_symlink()
        ),
        "user_id_configured": configured(zotero.get("user_id")),
    }


def get_runtime_policy(config: JsonObject | None = None) -> JsonObject:
    """Return canonical safe runtime config without environment selection."""
    source = load_config() if config is None else config
    return resolve_runtime_policy(source).config_value()


def load_config():
    """config.json 로드. 없으면 환경변수 폴백."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    else:
        _config_cache = {
            "zotero": {
                "api_key": os.environ.get("ZOTERO_API_KEY", ""),
                "email": os.environ.get("UNPAYWALL_EMAIL", ""),
                "collections": {},
            },
            "unpaywall_email": os.environ.get("UNPAYWALL_EMAIL", ""),
        }

    resolve_runtime_policy(_config_cache)
    return _config_cache


def get_zotero_api_key():
    cfg = load_config()
    return cfg.get("zotero", {}).get("api_key", "") or os.environ.get("ZOTERO_API_KEY", "")






def get_zotero_user_id():
    """Zotero API Key로 User ID를 자동 조회. 캐싱."""
    global _user_id_cache
    if _user_id_cache is not None:
        return _user_id_cache

    env_id = os.environ.get("ZOTERO_USER_ID", "")
    if env_id:
        _user_id_cache = env_id
        return env_id

    api_key = get_zotero_api_key()
    if not api_key:
        raise ValueError("Zotero API key not found. Set config.json or ZOTERO_API_KEY env var.")

    try:
        url = "https://api.zotero.org/keys/current"
        req = urllib.request.Request(url, headers={
            "Zotero-API-Key": api_key, "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
            data = json.load(resp)
        _user_id_cache = str(data.get("userID", ""))
        return _user_id_cache
    except Exception as e:
        raise ValueError(f"Failed to fetch Zotero User ID: {e}")


def _fetch_collection_keys():
    """Zotero에서 collection name → key 매핑을 조회. 캐싱."""
    global _collection_key_cache
    if _collection_key_cache is not None:
        return _collection_key_cache

    api_key = get_zotero_api_key()
    user_id = get_zotero_user_id()

    try:
        url = f"https://api.zotero.org/users/{user_id}/collections?format=json&limit=100"
        req = urllib.request.Request(url, headers={
            "Zotero-API-Key": api_key, "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
            cols = json.load(resp)
        _collection_key_cache = {c["data"]["name"]: c["data"]["key"] for c in cols}
        return _collection_key_cache
    except Exception as e:
        print(f"WARNING: Failed to fetch Zotero collections: {e}")
        _collection_key_cache = {}
        return _collection_key_cache


def _resolve_collection_value(value):
    """Collection value가 이름이면 key로 변환, 이미 key면 그대로.

    Zotero collection key는 8자 대문자 영숫자 (예: WKEZLEE8).
    "Humanoid"처럼 8자이면서 알파벳이 섞인 이름과 구분하기 위해
    먼저 이름으로 조회한다.
    """
    if not value:
        return ""
    # 이름으로 먼저 조회 (API 캐시)
    name_to_key = _fetch_collection_keys()
    if value in name_to_key:
        return name_to_key[value]
    # Zotero key 패턴: 8자 + 대문자/숫자만 (소문자 불가)
    if len(value) == 8 and value.isalnum() and not any(c.islower() for c in value):
        return value
    print(f"WARNING: Collection '{value}' not found in Zotero.")
    return value


def get_collections():
    """topic → collection key dict 반환. 이름은 자동으로 key로 변환."""
    cfg = load_config()
    raw = cfg.get("zotero", {}).get("collections", {})
    return {topic: _resolve_collection_value(val) for topic, val in raw.items()}


def get_collection_key(topic):
    return get_collections().get(topic, "")


def get_unpaywall_email():
    cfg = load_config()
    return cfg.get("unpaywall_email", "") or cfg.get("zotero", {}).get("email", "")


# ---------------------------------------------------------------------------
# 검색 키워드 (Core-1 search)
# ---------------------------------------------------------------------------
# config.json 최상위 "search_keywords".<topic> 가 우선. 없으면 아래 빌트인
# 기본값으로 폴백한다 (ai4s/scisci 는 설정 없이도 동작). 새 토픽은 config.json 에
# 블록을 추가하면 되고, 누락 시 get_search_keywords() 가 추가할 JSON 을 안내한다.

_DEFAULT_SEARCH_KEYWORDS = {
    "ai4s": {
        "primary": [
            "AI for science",
            "machine learning science",
            "scientific discovery AI",
            "neural network physics",
            "deep learning chemistry",
            "AI drug discovery",
            "scientific foundation model",
            "AI materials",
        ],
        "secondary": [
            "molecular dynamics",
            "protein structure",
            "weather prediction",
            "quantum chemistry",
            "scientific NLP",
            "research automation",
        ],
    },
    "scisci": {
        "primary": [
            "science of science",
            "bibliometrics",
            "scientometrics",
            "research evaluation",
            "citation analysis",
            "scientific collaboration",
        ],
        "secondary": [
            "h-index",
            "research impact",
            "academic careers",
            "peer review",
            "research funding",
            "open access",
            "reproducibility",
            "research trend",
            "international collaboration",
            "science mapping",
        ],
    },
}


def get_search_keywords(topic):
    """topic → {"primary": [...], "secondary": [...]} 검색 키워드 dict 반환.

    우선순위:
      1) config.json 최상위 "search_keywords".<topic>
      2) 빌트인 기본값 (_DEFAULT_SEARCH_KEYWORDS — ai4s/scisci)

    둘 다 없으면 config.json 에 그대로 붙여넣을 수 있는 JSON 블록을 담은
    ValueError 를 던진다.
    """
    cfg = load_config()
    configured = cfg.get("search_keywords", {}) or {}
    if topic in configured:
        return configured[topic]
    if topic in _DEFAULT_SEARCH_KEYWORDS:
        return _DEFAULT_SEARCH_KEYWORDS[topic]

    example_block = json.dumps(
        {
            "search_keywords": {
                topic: {
                    "primary": [
                        f"{topic} 핵심 키워드 1",
                        f"{topic} 핵심 키워드 2",
                        f"{topic} 핵심 키워드 3",
                    ],
                    "secondary": [
                        f"{topic} 보조 키워드 1",
                        f"{topic} 보조 키워드 2",
                    ],
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    raise ValueError(
        f"'{topic}' 토픽의 검색 키워드(search_keywords)가 정의되지 않았습니다.\n"
        f"config.json 최상위에 아래 \"search_keywords\" 블록을 추가하세요.\n"
        f"  - primary: 관련성 가중치가 높은 핵심 키워드 (제목/초록 매칭 0.5점)\n"
        f"  - secondary: 보조 키워드 (매칭 0.2점)\n\n"
        f"{example_block}"
    )


def get_paperbanana_dir():
    cfg = load_config()
    return cfg.get("paperbanana_dir", "")


def get_zotero_dir():
    """Zotero PDF 저장 디렉토리."""
    cfg = load_config()
    return (cfg.get("zotero", {}).get("pdf_dir", "")
            or os.environ.get("ZOTERO_DIR", ""))


def get_github_repo():
    """GitHub repo (owner/repo 형식)."""
    cfg = load_config()
    return (cfg.get("github", {}).get("repo", "")
            or os.environ.get("GITHUB_REPO", ""))


def get_github_branch():
    """GitHub branch (기본 master)."""
    cfg = load_config()
    return (cfg.get("github", {}).get("branch", "")
            or os.environ.get("GITHUB_BRANCH", "master"))


def get_pages_base_url():
    """GitHub Pages base URL."""
    cfg = load_config()
    return (cfg.get("github", {}).get("pages_base_url", "")
            or os.environ.get("PAGES_BASE_URL", ""))


def get_topic_dir(topic: str) -> Path:
    """docs/{topic} 경로 반환."""
    return DOCS_DIR / topic


def get_papers_index_path() -> Path:
    """papers/_papers_index.json 경로 반환."""
    return PAPERS_DIR / "_papers_index.json"
