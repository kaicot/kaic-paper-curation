"""Canonical license normalization + public-deploy reuse policy.

Single source of truth for the legal-risk gating on *public* (Cloudflare)
pages. Given a paper's license string (from OpenAlex, arXiv, or a CC URL),
this module answers three questions and provides a display label:

  - figure_public_ok(cls)  : may a public page reproduce the figure verbatim?
  - derivative_public_ok(cls): may the AI review (a derivative) be published?
  - label(cls)             : human attribution label (저작권법 제37조 / CC BY)

Rationale (KIST 대륙아주 법무검토의견서, 2026-02-27):
  - 모든 CC 라이선스는 *변경 없는* 원본의 재배포(복제·전송)를 허용 → figure(원본
    그대로)는 CC 이면 공개 OK. NC 도 사이트가 비영리면 준수로 본다.
  - ND(변경금지)만 2차적 저작물(=AI 리뷰) 작성/공개를 금지 → derivative 불가.
  - arXiv 비독점 배포 라이선스는 제3자 재배포까지 부여하지 않음 → 원본 figure
    공개는 명시적 근거 없음(strict 기본은 gate; 공정이용 판단 시 fairuse 로 완화).
"""

import re

# ── Canonical classes ────────────────────────────────────────────────────────
CC_BY        = "cc-by"
CC_BY_SA     = "cc-by-sa"
CC_BY_NC     = "cc-by-nc"
CC_BY_NC_SA  = "cc-by-nc-sa"
CC_BY_ND     = "cc-by-nd"
CC_BY_NC_ND  = "cc-by-nc-nd"
CC0          = "cc0"
ARXIV        = "arxiv"        # arXiv non-exclusive distribution (author retains ©)
OPENREVIEW   = "openreview"   # OpenReview OA submission (ICLR/ICML/NeurIPS 등)
PUBLISHER    = "publisher"    # all-rights-reserved / publisher-specific OA
UNKNOWN      = "unknown"

_CC_REDISTRIBUTABLE = {CC_BY, CC_BY_SA, CC_BY_NC, CC_BY_NC_SA, CC_BY_ND, CC_BY_NC_ND, CC0}
_ND = {CC_BY_ND, CC_BY_NC_ND}

_LABEL = {
    CC_BY: "CC BY", CC_BY_SA: "CC BY-SA", CC_BY_NC: "CC BY-NC",
    CC_BY_NC_SA: "CC BY-NC-SA", CC_BY_ND: "CC BY-ND", CC_BY_NC_ND: "CC BY-NC-ND",
    CC0: "CC0 / Public Domain", ARXIV: "arXiv 비독점 라이선스",
    OPENREVIEW: "OpenReview 공개(오픈액세스)",
    PUBLISHER: "출판사 보유(All rights reserved)", UNKNOWN: "라이선스 미상",
}


def normalize(raw):
    """Any license string/URL/token → canonical class. None/'' → UNKNOWN.

    Handles OpenAlex tokens ('cc-by-nc-nd', 'cc0', 'public-domain',
    'publisher-specific-oa'), CC URLs, and arXiv license URLs.
    """
    if not raw:
        return UNKNOWN
    s = str(raw).strip().lower()

    # arXiv non-exclusive distribution license
    if "arxiv.org/licenses/nonexclusive" in s or s in ("arxiv", "nonexclusive-distrib"):
        return ARXIV

    if "openreview" in s:
        return OPENREVIEW

    # CC0 / public domain
    if ("publicdomain/zero" in s or "publicdomain/mark" in s
            or s in ("cc0", "public-domain", "public-domain-mark", "pd", "cc-pd")):
        return CC0

    toks = set(t for t in re.split(r"[^a-z0-9]+", s) if t)
    looks_cc = ("creativecommons" in s or "cc-by" in s or s.startswith("cc-")
                or ("by" in toks and ({"nc", "nd", "sa"} & toks)))
    if looks_cc:
        nc, nd, sa = "nc" in toks, "nd" in toks, "sa" in toks
        if nd and nc:
            return CC_BY_NC_ND
        if nd:
            return CC_BY_ND
        if sa and nc:
            return CC_BY_NC_SA
        if sa:
            return CC_BY_SA
        if nc:
            return CC_BY_NC
        return CC_BY  # plain attribution

    # publisher-specific OA / all rights reserved / closed
    if ("publisher" in s or "all-rights" in s or "all rights reserved" in s
            or "other-closed" in s or s == "closed"):
        return PUBLISHER

    return UNKNOWN


def figure_public_ok(cls, *, strict=True):
    """May a PUBLIC page reproduce the figure verbatim?

    - Any CC / CC0 → True (redistribution of the unmodified work is licensed;
      NC ok because the deployed site is non-commercial).
    - strict (default): arXiv-default / unknown / publisher → False (no clear grant).
    - strict=False (fair-use stance): also allow arXiv (single figure, 비영리,
      출처표시, 원문 링크 → 공정이용 여지). unknown/publisher 는 항상 gate.
    """
    if cls in _CC_REDISTRIBUTABLE:
        return True
    if not strict and cls in (ARXIV, OPENREVIEW):
        return True
    return False


def derivative_public_ok(cls):
    """May the AI review (a derivative/adaptation) be published publicly?

    Only ND licenses forbid adaptations. arXiv/unknown/publisher rely on the
    fair-use stance (transformative, 비영리, OA 출처) and are allowed here.
    """
    return cls not in _ND


def is_nd(cls):
    return cls in _ND


def is_cc(cls):
    return cls in _CC_REDISTRIBUTABLE


def is_known(cls):
    return cls not in (UNKNOWN,)


def label(cls):
    return _LABEL.get(cls, _LABEL[UNKNOWN])
