"""bib_enrich — Zotero Wizard 수준 서지 메타데이터 enrichment.

DOI / arXiv ID 만 있어도 CrossRef + arXiv API 로 publicationTitle,
volume, issue, pages, ISSN, publisher, full author list, abstract 까지
채워서 Zotero 아이템 템플릿을 구성한다.

WDAC + V3 환경에서 Zotero translation-server (Docker) 를 띄울 수
없는 PC 를 위한 대체 경로. 같은 코드가 새 PC 에서 translation-server
도입 후에도 폴백으로 그대로 동작한다.
"""

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_SSL = ssl.create_default_context()

_ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return resp.read()


def extract_arxiv_id(*candidates):
    """문자열들에서 arXiv ID (예: 2509.12345) 추출. 첫 매치 반환."""
    for s in candidates:
        if not s:
            continue
        m = re.search(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})",
                      str(s), re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.match(r"^([0-9]{4}\.[0-9]{4,5})$", str(s).strip())
        if m:
            return m.group(1)
    return ""


def fetch_arxiv_metadata(arxiv_id, timeout=15):
    """arXiv API → 표준 dict. 실패 시 None.

    반환 키: title, abstract, date, authors (list[str]), category,
             arxiv_id, doi, journal_ref.
    """
    if not arxiv_id:
        return None
    url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id)}"
    try:
        raw = _http_get(url, headers={"User-Agent": "paper-curation/1.0"},
                        timeout=timeout)
    except Exception:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    entry = root.find("atom:entry", _ARXIV_NS)
    if entry is None:
        return None

    def _txt(tag, ns="atom"):
        el = entry.find(f"{ns}:{tag}", _ARXIV_NS)
        return (el.text or "").strip().replace("\n", " ") if el is not None else ""

    authors = [
        (a.findtext("atom:name", default="", namespaces=_ARXIV_NS) or "").strip()
        for a in entry.findall("atom:author", _ARXIV_NS)
    ]
    primary_cat = entry.find("arxiv:primary_category", _ARXIV_NS)
    category = primary_cat.attrib.get("term", "") if primary_cat is not None else ""

    return {
        "title": _txt("title"),
        "abstract": _txt("summary"),
        "date": _txt("published")[:10],
        "authors": [a for a in authors if a],
        "category": category,
        "arxiv_id": arxiv_id,
        "doi": _txt("doi", "arxiv"),
        "journal_ref": _txt("journal_ref", "arxiv"),
    }


def _strip_jats(text):
    """CrossRef abstract 의 <jats:p> 등 XML 태그 제거."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_crossref_metadata(doi, mailto=None, timeout=15):
    """CrossRef API → 표준 dict. 실패 시 None.

    반환 키: title, abstract, date, authors (list[dict{firstName,lastName}]),
             doi, publicationTitle, volume, issue, pages, publisher, ISSN,
             url, type ('journal-article' / 'posted-content' / 'proceedings-article' 등).
    """
    if not doi:
        return None
    safe_doi = urllib.parse.quote(doi.strip(), safe="")
    url = f"https://api.crossref.org/works/{safe_doi}"
    ua = "paper-curation/1.0"
    if mailto:
        ua += f" (mailto:{mailto})"
    try:
        raw = _http_get(url, headers={"User-Agent": ua}, timeout=timeout)
        msg = json.loads(raw.decode("utf-8")).get("message", {})
    except Exception:
        return None
    if not msg:
        return None

    title_arr = msg.get("title") or []
    container_arr = msg.get("container-title") or []
    issn_arr = msg.get("ISSN") or []
    issued = (msg.get("issued") or {}).get("date-parts") or [[]]
    issued = issued[0] if issued else []
    if issued:
        date = "-".join(
            f"{int(p):02d}" if i > 0 else str(int(p))
            for i, p in enumerate(issued) if p is not None
        )
    else:
        date = ""

    authors = []
    for a in msg.get("author") or []:
        gn = (a.get("given") or "").strip()
        fn = (a.get("family") or "").strip()
        if fn or gn:
            authors.append({"firstName": gn, "lastName": fn})

    isbn_arr = msg.get("ISBN") or []
    # CrossRef 'editor' for book/bookSection
    editors = []
    for e in msg.get("editor") or []:
        gn = (e.get("given") or "").strip()
        fn = (e.get("family") or "").strip()
        if fn or gn:
            editors.append({"firstName": gn, "lastName": fn})

    return {
        "title": (title_arr[0] if title_arr else "").strip(),
        "abstract": _strip_jats(msg.get("abstract", "")),
        "date": date,
        "authors": authors,
        "editors": editors,
        "doi": msg.get("DOI", doi),
        "publicationTitle": (container_arr[0] if container_arr else "").strip(),
        "volume": str(msg.get("volume", "")).strip(),
        "issue": str(msg.get("issue", "")).strip(),
        "pages": str(msg.get("page", "")).strip(),
        "publisher": str(msg.get("publisher", "")).strip(),
        "place": str(msg.get("publisher-location", "")).strip(),
        "ISSN": issn_arr[0] if issn_arr else "",
        "ISBN": isbn_arr[0] if isbn_arr else "",
        "edition": str(msg.get("edition-number", "")).strip(),
        "language": str(msg.get("language", "")).strip(),
        "url": msg.get("URL", ""),
        "type": msg.get("type", ""),
    }


def enrich(paper, mailto=None, sleep=0.4):
    """입력 paper dict 에 arXiv + CrossRef 결과 병합한 새 dict 반환.

    원본 dict 의 빈 필드만 채우며, 기존 값은 보존한다 (사용자 수정 안 덮어씀).
    출처 추적용 키:
      __source            : 'arxiv' / 'crossref' / 'arxiv+crossref'
      __crossref_type     : journal-article / posted-content / ...
    """
    out = dict(paper)
    sources = []

    arxiv_id = (
        out.get("arxiv_id")
        or out.get("arxivId")
        or extract_arxiv_id(out.get("url"), out.get("pdf_url"), out.get("doi"))
    )
    if arxiv_id:
        ax = fetch_arxiv_metadata(arxiv_id)
        time.sleep(sleep)
        if ax:
            out["arxiv_id"] = arxiv_id
            for k in ("title", "abstract", "date"):
                if not out.get(k) and ax.get(k):
                    out[k] = ax[k]
            if not out.get("authors") and ax.get("authors"):
                out["authors"] = ax["authors"]
            if ax.get("category") and "arxiv_category" not in out:
                out["arxiv_category"] = ax["category"]
            if ax.get("doi") and not (out.get("doi") or out.get("DOI")):
                out["doi"] = ax["doi"]
            sources.append("arxiv")

    doi = out.get("doi") or out.get("DOI") or ""
    if doi:
        cr = fetch_crossref_metadata(doi, mailto=mailto)
        time.sleep(sleep)
        if cr:
            for k in ("title", "abstract", "date"):
                if not out.get(k) and cr.get(k):
                    out[k] = cr[k]
            if not out.get("authors") and cr.get("authors"):
                out["authors"] = cr["authors"]
            if not out.get("editors") and cr.get("editors"):
                out["editors"] = cr["editors"]
            for k in ("publicationTitle", "volume", "issue", "pages",
                       "publisher", "ISSN", "place", "ISBN", "edition",
                       "language"):
                if not out.get(k) and cr.get(k):
                    out[k] = cr[k]
            if cr.get("type"):
                out["__crossref_type"] = cr["type"]
            sources.append("crossref")

    if sources:
        out["__source"] = "+".join(sources)
    return out


# Map CrossRef "type" → Zotero "itemType"
_CROSSREF_TYPE_MAP = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "book": "book",
    "monograph": "book",
    "report": "report",
    "dissertation": "thesis",
    "posted-content": "preprint",
    "preprint": "preprint",
}


def _parse_authors(authors_raw, creator_type="author"):
    """list[str] / list[dict] / str → Zotero creators list.

    creator_type: "author" | "editor" | "bookAuthor" 등 Zotero creatorType.
    """
    creators = []
    if not authors_raw:
        return creators
    if isinstance(authors_raw, str):
        authors_raw = [a.strip() for a in authors_raw.split(",") if a.strip()]
    for a in authors_raw:
        if isinstance(a, dict):
            if "lastName" in a or "firstName" in a:
                creators.append({
                    "creatorType": creator_type,
                    "firstName": a.get("firstName", ""),
                    "lastName": a.get("lastName", ""),
                })
            elif "name" in a:
                parts = a["name"].strip().split()
                creators.append({
                    "creatorType": creator_type,
                    "firstName": " ".join(parts[:-1]) if len(parts) > 1 else "",
                    "lastName": parts[-1] if parts else "",
                })
        elif isinstance(a, str):
            parts = a.strip().split()
            creators.append({
                "creatorType": creator_type,
                "firstName": " ".join(parts[:-1]) if len(parts) > 1 else "",
                "lastName": parts[-1] if parts else "",
            })
    return creators


def to_zotero_item(paper, collection_key=None):
    """enrich 완료된 paper dict → Zotero API item dict.

    itemType 결정 우선순위:
      1. paper["itemType"] (호출자 명시)
      2. arXiv ID 있으면 → preprint
      3. CrossRef type 매핑
      4. 기본값 journalArticle
    """
    arxiv_id = paper.get("arxiv_id") or extract_arxiv_id(
        paper.get("url"), paper.get("pdf_url")
    )
    cr_type = paper.get("__crossref_type", "")

    item_type = (
        paper.get("itemType")
        or (_CROSSREF_TYPE_MAP.get(cr_type) if cr_type else None)
        or ("preprint" if arxiv_id else "journalArticle")
    )

    doi = paper.get("doi") or paper.get("DOI") or ""
    url = paper.get("url") or paper.get("pdf_url") or ""
    if arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    elif not url and doi:
        url = f"https://doi.org/{doi}"

    date_val = paper.get("date") or paper.get("year") or paper.get("publicationDate") or ""
    if isinstance(date_val, int):
        date_val = str(date_val)

    creators = _parse_authors(paper.get("authors") or paper.get("author") or [])
    # bookSection: 챕터 저자(authors) + 책 편집자(editors) 모두 creators 에 합친다.
    if item_type == "bookSection" and paper.get("editors"):
        creators.extend(_parse_authors(paper.get("editors"), creator_type="editor"))
    # book: 편집자만 있는 경우(편저서)
    elif item_type == "book" and not creators and paper.get("editors"):
        creators = _parse_authors(paper.get("editors"), creator_type="editor")
    elif item_type == "book" and paper.get("editors"):
        creators.extend(_parse_authors(paper.get("editors"), creator_type="editor"))

    # 같은 사람이 author + editor 양쪽에 있으면 author 만 남긴다.
    if creators:
        author_keys = {
            (c.get("firstName", "").lower(), c.get("lastName", "").lower())
            for c in creators
            if c.get("creatorType") == "author"
        }
        creators = [
            c
            for c in creators
            if c.get("creatorType") != "editor"
            or (c.get("firstName", "").lower(), c.get("lastName", "").lower())
            not in author_keys
        ]

    item = {
        "itemType": item_type,
        "title": (paper.get("title") or "").strip(),
        "creators": creators,
        "abstractNote": paper.get("abstract") or paper.get("abstractNote") or "",
        "date": str(date_val),
        "url": url,
    }
    if doi:
        item["DOI"] = doi
    if collection_key:
        item["collections"] = [collection_key]

    if item_type == "preprint":
        item["repository"] = "arXiv" if arxiv_id else (paper.get("publisher") or "")
        if arxiv_id:
            item["archiveID"] = f"arXiv:{arxiv_id}"
    elif item_type == "journalArticle":
        for src, dst in (
            ("publicationTitle", "publicationTitle"),
            ("volume", "volume"),
            ("issue", "issue"),
            ("pages", "pages"),
            ("ISSN", "ISSN"),
        ):
            v = paper.get(src)
            if v:
                item[dst] = str(v)
    elif item_type == "conferencePaper":
        if paper.get("publicationTitle"):
            item["proceedingsTitle"] = paper["publicationTitle"]
        if paper.get("publisher"):
            item["publisher"] = paper["publisher"]
        if paper.get("pages"):
            item["pages"] = str(paper["pages"])
    elif item_type == "book":
        # Zotero book fields: publisher, place, ISBN, edition, numPages,
        # series, seriesNumber, volume, numberOfVolumes, language
        for src, dst in (
            ("publisher", "publisher"),
            ("place", "place"),
            ("ISBN", "ISBN"),
            ("edition", "edition"),
            ("numPages", "numPages"),
            ("series", "series"),
            ("seriesNumber", "seriesNumber"),
            ("volume", "volume"),
            ("numberOfVolumes", "numberOfVolumes"),
            ("language", "language"),
        ):
            v = paper.get(src)
            if v in (None, "", "0", 0):
                continue
            item[dst] = str(v)
    elif item_type == "bookSection":
        # Zotero bookSection fields: bookTitle, publisher, place, pages, ISBN,
        # edition, series, seriesNumber, volume, numberOfVolumes, language
        if paper.get("bookTitle"):
            item["bookTitle"] = paper["bookTitle"]
        elif paper.get("publicationTitle"):
            item["bookTitle"] = paper["publicationTitle"]
        for src, dst in (
            ("publisher", "publisher"),
            ("place", "place"),
            ("pages", "pages"),
            ("ISBN", "ISBN"),
            ("edition", "edition"),
            ("series", "series"),
            ("seriesNumber", "seriesNumber"),
            ("volume", "volume"),
            ("numberOfVolumes", "numberOfVolumes"),
            ("language", "language"),
        ):
            v = paper.get(src)
            if v in (None, "", "0", 0):
                continue
            item[dst] = str(v)

    return item
