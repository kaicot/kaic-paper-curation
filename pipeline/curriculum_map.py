#!/usr/bin/env python3
"""전체 커리큘럼 연구 지도 — 1~N강에 등장한 모든 논문을 한 캔버스에.

- Dashun Wang 그룹 논문 = 주황색, 그 외 그룹 = 같은 명도의 회색.
- 발표 연도 gradation: 오래될수록 밝게(the older, the brighter).
- 위치는 lecture_map.compute_layout 의 constellation(강별 centroid) 재사용.
- 등장 논문 = 각 강 gather_evidence 의 core(그룹) + 관련(top10) 합집합.

사용: python curriculum_map.py [out.png]
"""
import os, sys, colorsys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle, FancyBboxPatch
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lecture_digest as ald
import lecture_map as lm

ORANGE_H = 26.0 / 360.0          # 주황 hue
L_HI, L_LO = 0.86, 0.32          # 오래된(밝음) .. 최신(어두움)


def _iyear(v):
    try:
        return int(str(v)[:4])
    except Exception:
        return None



def wang_rgb(L):
    return colorsys.hls_to_rgb(ORANGE_H, L, 0.90)


def gray_rgb(L):
    return colorsys.hls_to_rgb(0.0, L, 0.0)


def is_wang(slug, coreset, idx):
    if slug in coreset:
        return True
    auth = idx.get(slug, {}).get("authors") or []
    return any("dashun wang" in (a or "").lower() for a in auth)


def build_curriculum_map(out_path):
    lm.set_korean_font()
    L = lm.compute_layout(1)                    # constellation-wide fields
    ppos, hub, nums, idx, edges = L["ppos"], L["hub"], L["nums"], L["idx"], L["edges"]
    title_of = L["title_of"]
    led = ald.load_ledger()
    course = led.get("course", "Dashun Wang 커리큘럼")

    # featured papers = union over lectures of (core + related top10)
    coreset, featured, year = set(), set(), {}
    for lec in led["lectures"]:
        _ev, core, related = ald.gather_evidence(lec)
        for c in core:
            coreset.add(c["slug"]); featured.add(c["slug"])
            if c.get("year"):
                year[c["slug"]] = _iyear(c["year"])
        for r in related:
            featured.add(r["slug"])
            if r.get("year") and r["slug"] not in year:
                year[r["slug"]] = _iyear(r["year"])
    # fill missing years from index
    for s in featured:
        if year.get(s) is None:
            year[s] = _iyear(lm._year(idx.get(s, {})))

    featured = [s for s in featured if s in ppos]
    wang = [s for s in featured if is_wang(s, coreset, idx)]
    other = [s for s in featured if s not in set(wang)]
    yvals = [year[s] for s in featured if year.get(s)]
    ymin, ymax = min(yvals), max(yvals)
    # gradation by publication ORDER (distinct-year rank), evenly spread so a
    # single old outlier doesn't compress the scale. older -> brighter.
    distinct = sorted(set(yvals))
    _rank = {y: i for i, y in enumerate(distinct)}

    def Lof(y):
        if y is None or len(distinct) <= 1:
            return (L_HI + L_LO) / 2
        return L_HI - (_rank[y] / (len(distinct) - 1)) * (L_HI - L_LO)

    # ── 네트워크를 1:1 정사각으로 채워 중앙 정렬 (x·y 독립 스케일) ─────────────
    pts = np.array([ppos[s] for s in featured], dtype=float)
    cmin, cmax = pts.min(0), pts.max(0)
    cloud_c = (cmin + cmax) / 2.0
    cspan = cmax - cmin                                     # [dx, dy]
    TARGET = 0.72                                           # 네트워크가 채우는 정사각 변 길이
    sx = TARGET / (cspan[0] or 1.0)
    sy = TARGET / (cspan[1] or 1.0)                         # x·y 독립 → bbox 가 정사각(1:1)
    CENTER = np.array([0.5, 0.5])

    def T(p):
        d = np.asarray(p, float) - cloud_c
        return CENTER + np.array([d[0] * sx, d[1] * sy])

    tppos = {s: T(ppos[s]) for s in ppos}
    thub = {n: T(hub[n]) for n in nums}

    fig = plt.figure(figsize=(11.6, 11.6), dpi=160)        # 1:1
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_aspect("equal"); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor="white", zorder=0))

    def Wof(y, lo, hi):
        """연도 → 선 굵기: 오래된=가늘게, 최신=굵게."""
        if y is None or len(distinct) <= 1:
            return (lo + hi) / 2
        return lo + (_rank[y] / (len(distinct) - 1)) * (hi - lo)

    wangset, otherset = set(wang), set(other)
    # (1) 기타 → 연관 Wang 논문 연결선 (회색, 연도 gradation · 최신일수록 굵게)
    g_segs, g_cols, g_lws = [], [], []
    for a, b in edges:
        if a in wangset and b in otherset:
            g, w = b, a
        elif a in otherset and b in wangset:
            g, w = a, b
        else:
            continue
        g_segs.append([tppos[g], tppos[w]])
        g_cols.append(gray_rgb(Lof(year[g])))
        g_lws.append(Wof(year[g], 0.25, 1.5))
    ax.add_collection(LineCollection(g_segs, colors=g_cols, linewidths=g_lws,
                                     alpha=0.33, zorder=1))
    # (2) Wang 시간순 연결선 (주황 spine, 연도 gradation · 최신일수록 굵게)
    wsorted = sorted([w for w in wang if year.get(w)], key=lambda w: (year[w], w))
    s_segs, s_cols, s_lws = [], [], []
    for i in range(len(wsorted) - 1):
        yb = year[wsorted[i + 1]]                           # 더 최신 쪽 끝점 기준
        s_segs.append([tppos[wsorted[i]], tppos[wsorted[i + 1]]])
        s_cols.append(wang_rgb(Lof(yb)))
        s_lws.append(Wof(yb, 0.7, 3.4))
    ax.add_collection(LineCollection(s_segs, colors=s_cols, linewidths=s_lws,
                                     alpha=0.8, zorder=2, capstyle="round"))

    # nodes: gray (other) behind, orange (Wang) in front
    for s in other:
        c = gray_rgb(Lof(year[s])); pp = tppos[s]
        ax.scatter([pp[0]], [pp[1]], s=46, facecolor=[c], edgecolors="#FFFFFF",
                   linewidths=0.5, alpha=0.96, zorder=3)
    for s in wang:
        Lv = Lof(year[s])
        c = wang_rgb(Lv); ec = wang_rgb(max(0.16, Lv - 0.22)); pp = tppos[s]
        ax.scatter([pp[0]], [pp[1]], s=92, facecolor=[c], edgecolors=[ec],
                   linewidths=0.9, zorder=5)
    # 각 강 군집 중심에 흐린 번호(리더선이 향하는 지점)
    for n in nums:
        h = thub[n]
        ax.text(h[0], h[1], str(n), fontsize=15, color="#E3E7EB",
                fontweight="bold", ha="center", va="center", zorder=2)

    # ── 주변 네모박스: 제N강 + 제목 (강 순서대로 원형 배치 · 리더선 연결) ────────
    def wrap_title(t, width=12, maxlines=2):
        t = " ".join((t or "").split())
        lines = [t[i:i + width] for i in range(0, len(t), width)][:maxlines]
        if lines and len("".join(lines)) < len(t):
            lines[-1] = lines[-1][:max(1, width - 1)] + "…"
        return "\n".join(lines)

    R_ring, bw, bh = 0.375, 0.152, 0.070
    slots = []
    for i, n in enumerate(nums):
        th = np.pi / 2 - 2 * np.pi * i / len(nums)          # 1강 상단, 시계방향
        slots.append((n, 0.5 + R_ring * np.cos(th), 0.5 + R_ring * np.sin(th)))
    # 리더선 먼저(박스가 위에 덮이도록)
    for n, bxc, byc in slots:
        hx, hy = thub[n]
        ax.plot([bxc, hx], [byc, hy], color="#C7CDD3", lw=0.8, alpha=0.6,
                zorder=1.6, solid_capstyle="round")
    # 네모박스 + 텍스트
    for n, bxc, byc in slots:
        ax.add_patch(FancyBboxPatch((bxc - bw / 2, byc - bh / 2), bw, bh,
                     boxstyle="round,pad=0.006,rounding_size=0.012",
                     fc="#FFFFFF", ec=wang_rgb(0.45), lw=1.15, alpha=0.5, zorder=8))
        ax.text(bxc, byc + bh * 0.26, "제%d강" % n, fontsize=9.2, fontweight="bold",
                color=wang_rgb(0.28), ha="center", va="center", zorder=9)
        ax.text(bxc, byc - bh * 0.17, wrap_title(title_of[n]), fontsize=7.1,
                color="#212529", ha="center", va="center", linespacing=1.18, zorder=9)

    # ---- title (상단 중앙) ----
    ax.text(0.5, 0.987, "%s — 전체 연구 지도" % course, fontsize=17,
            fontweight="bold", color="#212529", ha="center", va="top")
    ax.text(0.5, 0.960,
            "논문 %d편 (Wang 그룹 %d · 타 그룹 %d) · %d–%d"
            % (len(featured), len(wang), len(other), ymin, ymax),
            fontsize=10.5, color="#868E96", ha="center", va="top")

    # ---- 하단 가로 컬러바 (연도: 오래될수록 밝게 · 최신일수록 진하게) ----
    from matplotlib.colors import LinearSegmentedColormap
    grad = np.linspace(0.0, 1.0, 256)                          # 0=oldest .. 1=newest
    cols = [wang_rgb(L_HI - t * (L_HI - L_LO)) for t in grad]   # 왼쪽 밝음(old) .. 오른쪽 진함(new)
    cmap = LinearSegmentedColormap.from_list("wang_year", cols)
    cax = fig.add_axes([0.32, 0.040, 0.36, 0.013])             # 가늘고 긴 바(하단 중앙)
    cax.imshow(grad.reshape(1, -1), aspect="auto", cmap=cmap, extent=[0, 1, 0, 1])
    cax.set_yticks([]); cax.set_xlim(0, 1)
    nd = len(distinct)
    if nd > 1:
        fr = [0.0, 0.25, 0.5, 0.75, 1.0]
        cax.set_xticks(fr)
        cax.set_xticklabels([str(distinct[round(f * (nd - 1))]) for f in fr],
                            fontsize=8.5, color="#495057")
    else:
        cax.set_xticks([])
    cax.tick_params(length=3, pad=2.5, colors="#868E96")
    for sp in cax.spines.values():
        sp.set_edgecolor("#CED4DA"); sp.set_linewidth(0.6)
    cax.text(0.5, 2.1, "논문 연도  —  오래될수록 밝게 · 최신일수록 진하고 굵게",
             fontsize=9.5, color="#495057", ha="center", va="bottom",
             transform=cax.transAxes)

    fig.savefig(out_path, facecolor="white")
    print("saved", out_path, os.path.getsize(out_path), "bytes")
    print("featured=%d wang=%d other=%d years=%d-%d"
          % (len(featured), len(wang), len(other), ymin, ymax))
    return out_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "curriculum_map.png"
    build_curriculum_map(out)
