#!/usr/bin/env python3
"""Dashun Wang curriculum map for a lecture (this-lecture focus).

Every paper is placed at the centroid of the lectures it touches (core
membership OR a 1-hop connection), giving a calm constellation of the whole
course; faint grey lines trace the connection web among all papers. On top of
that, THIS lecture's papers are foregrounded in orange: the papers actually
cited in the broadcast (core + related) are numbered in citation order and
joined by an orange thread — the reading path of the episode. Lecture hubs are
intentionally not drawn; the constellation and this-lecture highlight carry the
signal.

Three entry points share one layout (all accept an optional report_text so the
pipeline can pass the report before it is written to disk):
  - build_map(current, out_png[, report_text])        -> static PNG (email image)
  - interactive_block(current[, report_text])          -> HTML <div>: interactive
        inline SVG (hover = title+year, click = open paper) + linked paper list
  - assemble_report_html(current, raw_html[, report_text]) -> report HTML with the
        map placed below the title and redundant paper cards / connection map removed

Usage (standalone): lecture_map.py <current_lecture_n> <out_png> [out_html]
"""
import html as H
import math
import os
import re
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.collections import LineCollection
import numpy as np

sys.path.insert(0, "/Users/jehyunlee/Documents/paper-curation/pipeline")
import agent_lecture_digest as ald

ORANGE = "#F76707"
ORANGE_DK = "#B34700"
NAVY = "#1C3D5A"
GREY = "#CED4DA"
WEB = "#C4CAD0"
INK = "#212529"
SUB = "#868E96"
LINK = "#1971C2"


def set_korean_font():
    for c in ["/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "/System/Library/Fonts/Supplemental/AppleGothic.ttf"]:
        if os.path.exists(c):
            try:
                fm.fontManager.addfont(c)
                plt.rcParams["font.family"] = fm.FontProperties(fname=c).get_name()
                plt.rcParams["axes.unicode_minus"] = False
                return
            except Exception:
                pass


def short_label(title, n=8):
    t = re.sub(r"<[^>]+>", "", (title or "")).strip()
    for d in ["(", "·", ":", ", ", " 그리고", "그리고", "와 ", "과 "]:
        if d in t:
            t = t.split(d)[0].strip()
            break
    return t.rstrip(" ,의")[:n]


def _year(e):
    return e.get("year") or (e.get("date") or "")[:4] or ""


def appearing_papers(current, core, conns, idx, report_text=None):
    """Papers cited in this lecture's broadcast, in citation order."""
    if report_text is None:
        rp = ald.OUTDIR / ("lecture_%02d_report.md" % current)
        report_text = rp.read_text(encoding="utf-8") if rp.exists() else ""
    rl = report_text.lower()
    coreset = set(core)
    cand = list(core)
    rel_of = {}
    for s in core:
        for e in conns.get(s, []):
            rs = e.get("slug")
            if not rs:
                continue
            if rs not in cand:
                cand.append(rs)
            rel = e.get("relation")
            rsn = (e.get("reason") or "").strip()
            cur = rel_of.get(rs)
            if (cur is None
                    or (rel in ("counterpoint", "alternative") and cur[0] not in ("counterpoint", "alternative"))
                    or (not cur[1] and rsn)):
                rel_of[rs] = (rel, rsn)

    def firstpos(title):
        t = (title or "").strip()
        for cut in (len(t), 60, 45, 32):
            key = t[:cut].lower().strip()
            if len(key) < 12:
                break
            p = rl.find(key)
            if p >= 0:
                return p
        return -1

    rows, seen = [], set()
    for s in cand:
        e = idx.get(s, {})
        title = e.get("title") or ""
        pos = firstpos(title)
        if pos < 0:
            continue
        dk = (title.strip().lower(), str(_year(e)))
        if dk in seen:
            continue
        seen.add(dk)
        meta = rel_of.get(s, (None, ""))
        rows.append({"pos": pos, "slug": s, "title": title, "year": _year(e),
                     "core": s in coreset, "rel": meta[0], "reason": meta[1]})
    rows.sort(key=lambda r: r["pos"])
    return rows


def compute_layout(current, report_text=None):
    led = ald.load_ledger()
    conns = ald.all_connections()
    idx = ald.pidx()
    lectures = sorted(led["lectures"], key=lambda L: L["lecture"])
    nums = [L["lecture"] for L in lectures]
    title_of = {L["lecture"]: L["title"] for L in lectures}
    lec_core = {L["lecture"]: set(p["slug"] for p in L["papers"]) for L in lectures}
    group = set().union(*lec_core.values())
    curL = next(L for L in lectures if L["lecture"] == current)
    core = [p["slug"] for p in curL["papers"]]

    span = {}
    for n in nums:
        for s in lec_core[n]:
            span.setdefault(s, set()).add(n)
            for e in conns.get(s, []):
                rs = e.get("slug")
                if rs:
                    span.setdefault(rs, set()).add(n)

    appearing = appearing_papers(current, core, conns, idx, report_text)
    for r in appearing:
        r["link"] = ald.paper_link(r["slug"])
    cite_rank = {r["slug"]: i + 1 for i, r in enumerate(appearing)}
    appset = set(cite_rank)

    cx, cy, Rx, Ry = 0.275, 0.47, 0.215, 0.385
    hub = {}
    for i, n in enumerate(nums):
        th = math.pi / 2 - 2 * math.pi * i / len(nums)
        hub[n] = np.array([cx + Rx * math.cos(th), cy + Ry * math.sin(th)])

    rng = np.random.default_rng(7)
    C = np.array([cx, cy])
    ppos = {}
    for p, ls in span.items():
        m = np.mean([hub[n] for n in ls], axis=0)
        pos = C + (m - C) * (0.92 if len(ls) == 1 else 0.72)
        ppos[p] = pos + rng.normal(0, 0.02, 2)
    for r in appearing:
        ppos.setdefault(r["slug"], C + rng.normal(0, 0.05, 2))

    # connection web among all positioned papers (undirected, deduped)
    edges, seen = [], set()
    for s, es in conns.items():
        if s not in ppos:
            continue
        for e in es:
            rs = e.get("slug")
            if rs and rs in ppos:
                k = (s, rs) if s < rs else (rs, s)
                if k not in seen:
                    seen.add(k)
                    edges.append(k)

    return {"nums": nums, "title_of": title_of, "group": group, "core": core,
            "span": span, "appearing": appearing, "cite_rank": cite_rank,
            "appset": appset, "hub": hub, "ppos": ppos, "edges": edges,
            "idx": idx, "cx": cx, "cy": cy, "current": current}


def build_map(current, out_path, report_text=None):
    set_korean_font()
    L = compute_layout(current, report_text)
    hub, ppos, span, group = L["hub"], L["ppos"], L["span"], L["group"]
    appearing, cite_rank, appset = L["appearing"], L["cite_rank"], L["appset"]
    nums, title_of, idx = L["nums"], L["title_of"], L["idx"]
    cx, cy = L["cx"], L["cy"]

    fig, ax = plt.subplots(figsize=(15.5, 9.2), dpi=150)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # faint connection web among all papers
    segs = [[ppos[a], ppos[b]] for a, b in L["edges"]]
    ax.add_collection(LineCollection(segs, colors=WEB, linewidths=0.4,
                                     alpha=0.16, zorder=1))

    # background papers (not in this lecture) — visible, not dust
    for p, ls in span.items():
        if p in appset:
            continue
        pp = ppos[p]
        if p in group:
            ax.scatter([pp[0]], [pp[1]], s=34, facecolor=NAVY, edgecolors="white",
                       linewidths=0.6, alpha=0.65, zorder=4)
        else:
            ax.scatter([pp[0]], [pp[1]], s=20, facecolor="white", edgecolors="#AEB4BB",
                       linewidths=0.9, alpha=0.8, zorder=3)

    # orange citation thread through this lecture's papers (broadcast order)
    xs = [ppos[r["slug"]][0] for r in appearing]
    ys = [ppos[r["slug"]][1] for r in appearing]
    if len(xs) >= 2:
        ax.plot(xs, ys, color=ORANGE, lw=1.6, alpha=0.6, zorder=6,
                solid_capstyle="round", solid_joinstyle="round")

    # this-lecture nodes (all orange) + broadcast-order numbers
    for r in appearing:
        s = r["slug"]
        pp = ppos[s]
        if r["core"]:
            fc, ec, lw, sz, tc = ORANGE, ORANGE_DK, 1.4, 175, "white"
        else:
            fc, ec, lw, sz, tc = "#FFE8D6", ORANGE, 1.8, 140, ORANGE_DK
        ax.scatter([pp[0]], [pp[1]], s=sz, facecolor=fc, edgecolors=ec,
                   linewidths=lw, zorder=7)
        ax.text(pp[0], pp[1], str(cite_rank[s]), fontsize=7.6, fontweight="bold",
                ha="center", va="center", color=tc, zorder=9)

    # (lecture hubs intentionally omitted — clean literature constellation)

    # title + legend (top-left)
    ax.text(0.02, 0.985, "%d. %s" % (current, title_of[current]),
            fontsize=13.5, fontweight="bold", color=INK, va="top")
    ax.text(0.02, 0.945,
            "● 주황 = 이번 강의 논문 (진한색 = 핵심)   ·   선 = 연관문헌   ·   숫자 = 방송 인용 순서",
            fontsize=9.2, color=SUB, va="top")

    # ---- side panel: papers appearing in this lecture (broadcast order) ----
    px = 0.65
    ax.text(px, 0.955, "이번 강의에 등장하는 논문", fontsize=13.5,
            fontweight="bold", color=INK, va="top")
    ax.text(px, 0.921, "방송 인용 순서 · [그룹]=Wang 그룹 · [연관]=관련 연구",
            fontsize=8.6, color=SUB, va="top")
    y = 0.892
    for r in appearing:
        s = r["slug"]
        typ = "그룹" if r["core"] else "연관"
        head = "%d. [%s] %s (%s)" % (cite_rank[s], typ, r["title"], r["year"])
        lines = textwrap.wrap(head, width=60)[:3]
        for j, ln in enumerate(lines):
            ax.text(px, y, ln, fontsize=7.9, color=INK, va="top",
                    fontweight=("semibold" if j == 0 else "normal"))
            y -= 0.0206
        link = r["link"]
        if link.startswith("https://doi.org/"):
            disp = "doi.org/" + link[len("https://doi.org/"):]
        elif link.startswith("https://scholar.google.com"):
            disp = "Google Scholar 검색 링크"
        else:
            disp = link
        ax.text(px + 0.012, y, disp, fontsize=7.1, color=LINK, va="top")
        y -= 0.0245

    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    print("saved", out_path, os.path.getsize(out_path), "bytes")


# ---------------------------------------------------------------------------
# Interactive SVG (for the HTML report): hover = title+year, click = open paper.
# ---------------------------------------------------------------------------
_W, _Ht = 1000.0, 900.0


def _sx(x):
    return round(x * 1550.0 - 40.0, 1)


def _sy(y):
    return round((1 - y) * 900.0 + 12.0, 1)


def _svg_map(L):
    hub, ppos, span, group = L["hub"], L["ppos"], L["span"], L["group"]
    appearing, cite_rank, appset = L["appearing"], L["cite_rank"], L["appset"]
    nums, title_of, idx = L["nums"], L["title_of"], L["idx"]
    current = L["current"]
    out = []
    out.append(
        '<svg viewBox="-150 0 1210 912" width="100%" '
        'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'font-family="Apple SD Gothic Neo, Malgun Gothic, sans-serif" '
        'style="max-width:860px;display:block">')
    out.append('<rect x="-150" y="0" width="1210" height="912" fill="white"/>')

    # connection web
    for a, b in L["edges"]:
        pa, pb = ppos[a], ppos[b]
        out.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" '
                   'stroke-width="0.5" stroke-opacity="0.18"/>'
                   % (_sx(pa[0]), _sy(pa[1]), _sx(pb[0]), _sy(pb[1]), WEB))

    # orange citation thread
    pts = " ".join("%s,%s" % (_sx(ppos[r["slug"]][0]), _sy(ppos[r["slug"]][1]))
                   for r in appearing)
    out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.2" '
               'stroke-opacity="0.6" stroke-linejoin="round" stroke-linecap="round"/>'
               % (pts, ORANGE))

    def node(slug, cx, cy, r, fill, stroke, sw, title, year, link, num=None, tcolor=None):
        tip = H.escape("%s (%s)" % (title, year)) if year else H.escape(title)
        s = ['<a xlink:href="%s" href="%s" target="_blank" rel="noopener">'
             % (H.escape(link, quote=True), H.escape(link, quote=True))]
        s.append('<title>%s</title>' % tip)
        s.append('<circle cx="%s" cy="%s" r="%s" fill="%s" stroke="%s" '
                 'stroke-width="%s"/>' % (cx, cy, r, fill, stroke, sw))
        if num is not None:
            s.append('<text x="%s" y="%s" text-anchor="middle" dominant-baseline="central" '
                     'font-size="11" font-weight="700" fill="%s">%d</text>'
                     % (cx, cy + 0.5, tcolor, num))
        s.append('</a>')
        return "".join(s)

    # background papers (interactive too)
    for p, ls in span.items():
        if p in appset:
            continue
        pp = ppos[p]
        e = idx.get(p, {})
        link = ald.paper_link(p)
        if p in group:
            out.append(node(p, _sx(pp[0]), _sy(pp[1]), 6.5, NAVY, "white", 0.8,
                            e.get("title") or p, _year(e), link))
        else:
            out.append(node(p, _sx(pp[0]), _sy(pp[1]), 5.0, "white", "#AEB4BB", 1.0,
                            e.get("title") or p, _year(e), link))

    # this-lecture nodes (orange) + numbers
    for r in appearing:
        pp = ppos[r["slug"]]
        if r["core"]:
            fill, stroke, tc = ORANGE, ORANGE_DK, "white"
        else:
            fill, stroke, tc = "#FFE1CC", ORANGE, ORANGE_DK
        out.append(node(r["slug"], _sx(pp[0]), _sy(pp[1]), 13.0, fill, stroke, 2.0,
                        r["title"], r["year"], r["link"],
                        num=cite_rank[r["slug"]], tcolor=tc))

    # (lecture hubs intentionally omitted — clean literature constellation)

    out.append('</svg>')
    return "".join(out)


def interactive_block(current, report_text=None):
    """Return an HTML <div> with the interactive SVG map + a linked paper list."""
    L = compute_layout(current, report_text)
    svg = _svg_map(L)
    items = []
    rel_short = {"foundation": "기반", "extension": "확장", "alternative": "대안",
                 "application": "응용", "counterpoint": "반론"}
    for r in L["appearing"]:
        typ = "그룹" if r["core"] else "연관"
        head = (
            '<span style="color:%s;font-weight:700">[%s]</span> '
            '<a href="%s" target="_blank" rel="noopener" style="color:%s;text-decoration:none;font-weight:600">%s</a> '
            '<span style="color:#888">(%s)</span>'
            % (ORANGE if r["core"] else SUB, typ, H.escape(r["link"], quote=True),
               LINK, H.escape(r["title"]), H.escape(str(r["year"]))))
        desc = ""
        if not r["core"]:
            rs = rel_short.get(r["rel"], r["rel"] or "연관")
            reason = (r.get("reason") or "").strip()
            inner = ("(%s) %s" % (rs, H.escape(reason[:200]))) if reason else ("(%s)" % rs)
            desc = ('<div style="color:#555;font-size:.85em;line-height:1.45;'
                    'margin:.15em 0 .1em">%s</div>' % inner)
        items.append('<li style="margin:7px 0">%s%s</li>' % (head, desc))
    ol = ('<ol style="flex:1 1 360px;min-width:290px;margin:6px 0 0;padding-left:1.4em;'
          'font-size:.9rem;line-height:1.45;color:#222">%s</ol>' % "".join(items))
    ltitle = H.escape(L["title_of"][current])
    return (
        '<div style="border:1px solid #eee;border-radius:12px;padding:14px 14px 6px;margin:0 0 20px">'
        '<p style="margin:0 0 4px;font-weight:700;font-size:1.05rem">%d. %s</p>'
        '<p style="margin:0 0 10px;color:#888;font-size:.82rem">'
        '<b style="color:%s">주황 = 이번 강의 논문</b> (진한색 = 핵심) · <b>선 = 연관문헌</b> · 숫자 = 방송 인용 순서. '
        '점 위에 마우스를 올리면 제목·연도가 뜨고, 클릭하면 해당 논문으로 이동합니다.</p>'
        '<div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start">'
        '<div style="flex:2 1 520px;min-width:320px">%s</div>%s</div></div>'
        % (current, ltitle, ORANGE, svg, ol))


def assemble_report_html(current, raw_html, report_text=None):
    """Place the interactive map just below the title and drop the redundant
    core-paper card, related-research card, and the trailing connection map."""
    h = raw_html
    h = re.sub(r'<div class="card"><h3>📄 다루는 논문.*?</ul>\s*</div>', "", h, flags=re.S)
    h = re.sub(r'<div class="card"><h3>🔗 함께 보는 관련 연구.*?</ul>\s*</div>', "", h, flags=re.S)
    h = re.sub(r'<section class="connection-map">.*?</section>', "", h, flags=re.S)
    block = interactive_block(current, report_text)
    marker = '<div class="card"><h3>🎯 오늘의 학습목표'
    if marker in h:
        return h.replace(marker, block + marker, 1)
    return re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + block, h, count=1)


if __name__ == "__main__":
    cur = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/lecture_map.png"
    build_map(cur, out)
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:20px'>"
                    + interactive_block(cur) + "</body>")
        print("wrote interactive html", sys.argv[3])
