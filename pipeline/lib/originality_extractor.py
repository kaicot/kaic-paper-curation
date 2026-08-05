"""
Originality extraction from paper text.
Ported from scisci/scie/lib/originality.py.

Strategy:
1. Primary: rule-based trigger matching (free, instant)
2. No match: return an empty string without external work
"""
import json
import re
from pathlib import Path

TRIGGERS_PATH = Path(__file__).parent / "originality_triggers.json"


# ── Metadata leak strip ──
# originality.md 에 PDF 추출 잔재 (DOI, arXiv id, URL, HTML 태그) 가 섞여
# 들어가면 다운스트림 c-TF-IDF 키워드 추출 시 *클러스터 구별 단어* 로
# 부각되어 카테고리 이름 품질을 망친다. 모든 추출 경로의 마지막에서 적용.
_LEAK_PATTERNS = [
    # URL — 다음에 등장하는 DOI/arXiv 패턴이 URL 안에 포함되어 있어도 먼저 제거
    re.compile(r"https?://\S+", re.I),
    # arXiv ID (arXiv:2407.09811v1 / 2407.09811v1 / abs/2407.09811)
    re.compile(r"\b(?:arXiv:|abs/)?\d{4}\.\d{4,5}(?:v\d+)?\b", re.I),
    # DOI (10.NNNN/...)
    re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I),
    # HTML 태그 (<br>, <p>, <span>, ...)
    re.compile(r"<[a-zA-Z][^>]*>"),
]


def _strip_metadata_leaks(text: str) -> str:
    """Remove URL/arXiv/DOI/HTML leaks from extracted originality text.

    Idempotent. Returns the cleaned text with collapsed whitespace.
    """
    if not text:
        return text
    for pat in _LEAK_PATTERNS:
        text = pat.sub(" ", text)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_triggers(path=None):
    """Load trigger categories and flat list."""
    path = path or TRIGGERS_PATH
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    categories = {k: v for k, v in data.items() if k.startswith("rule_base_")}
    all_triggers = []
    for words in categories.values():
        all_triggers.extend(words)
    return {"categories": categories, "all": list(set(all_triggers)), "_path": str(path)}


def split_sentences(text):
    # Normalize: ligatures, non-breaking space, newlines, copyright symbol
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("\u00a9", " ").replace("\xa0", " ").replace("\n", " ")
    text = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 10]


# Strong novelty signals
_STRONG_NOVELTY = frozenset({
    "for the first time", "unprecedented", "pioneering",
    "state-of-the-art", "cutting-edge", "innovative",
})

_STRICT_AUTHORSHIP = frozenset({
    "we ", " our ", "this study", "this paper", "this work",
    "this article", "this research", "this report", "this investigation",
    "in this study", "in this work", "in this paper",
    "here ", "herein",
    "the paper ", "the study ", "the work ", "the article ",
    "the present study", "the present work", "the present paper",
    "the current study", "the current work", "the current paper",
})

_REFERENTIAL_STARTS = ("it ", "its ", "this ", "these ", "such ", "the ")

# Stop triggers (too broad to learn)
_STOP_TRIGGERS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "must", "need", "also",
    "not", "no", "but", "and", "or", "if", "then", "than", "that", "this",
    "these", "those", "it", "its", "they", "their", "them",
    "with", "from", "into", "for", "of", "on", "in", "at",
    "to", "by", "as", "about", "between", "through", "during",
    "more", "most", "very", "much", "many", "some", "any", "all",
    "based on", "due to", "in order to", "according to",
    "important", "significant", "recent", "various", "different",
    "however", "therefore", "thus", "hence", "moreover",
    "data", "method", "model", "system", "paper", "study", "research",
})


def _extract_rule_based(text, triggers):
    """Rule-based originality extraction with strict co-occurrence."""
    if not text or not text.strip():
        return ""

    content_categories = {k: v for k, v in triggers["categories"].items()
                          if "authorship" not in k}

    sentences = split_sentences(text)
    first_orig_idx = None

    for i, sentence in enumerate(sentences):
        s_lower = sentence.lower()
        has_strong = any(t in s_lower for t in _STRONG_NOVELTY)
        has_authorship = any(t in s_lower for t in _STRICT_AUTHORSHIP)
        has_content = False
        if has_authorship:
            for words in content_categories.values():
                for w in words:
                    if w in s_lower:
                        has_content = True
                        break
                if has_content:
                    break
        if has_strong or (has_authorship and has_content):
            first_orig_idx = i
            break

    if first_orig_idx is None:
        return ""

    start_idx = first_orig_idx
    if first_orig_idx > 0:
        s_lower = sentences[first_orig_idx].lower().lstrip()
        if any(s_lower.startswith(ref) for ref in _REFERENTIAL_STARTS):
            start_idx = first_orig_idx - 1

    return _strip_metadata_leaks(". ".join(sentences[start_idx:]))


# ── LLM Fallback ──

LLM_PROMPT = """Given the following scientific paper text, identify sentences
that describe the paper's originality, novelty, or unique contribution.

Return a JSON object with:
{{
  "originality_sentences": ["exact sentence 1 from text", "exact sentence 2", ...],
  "trigger_phrases": ["phrase that signals originality 1", "phrase 2", ...]
}}

Rules:
- "originality_sentences" must be EXACT copies of sentences from the text (no paraphrasing).
- "trigger_phrases" must be 1-3 word phrases FROM those sentences that signal authorship or novelty
  (e.g., "we report", "novel approach", "for the first time").
- Each trigger_phrase should be lowercase.
- If no originality is found, return empty lists.

Text:
{text}
"""




def extract_originality(text, triggers=None):
    """Return deterministic trigger-matched originality sentences."""
    if not text or not text.strip():
        return ""
    if triggers is None:
        triggers = load_triggers()
    return _extract_rule_based(text, triggers)
