#!/usr/bin/env python3
"""Dashun Wang 연구 흐름 타임라인 — 주제 스트림 + 주제 간 '공유·연결 논문' 관계망을 한 장에.

- 좌측 '기반' 블록에서 8개 주제 스트림이 갈라져 나와 연도순(왼→오른쪽)으로 흐른다.
- 각 논문 = 발표연도 위치의 노드. 노드 크기 = 다른 주제와 연결된 정도(교차 연결 수).
- 서로 다른 주제를 잇는 '공유·연결 논문' 관계는 곡선(arc)으로 드러낸다 → 주제 간 겹침이 보임.
- 여러 주제에 동시에 속한 논문은 두 레인 사이에 놓고 각 주제로 이어 붙인다.
- 우측 개별 논문 라벨 없음. aspect ratio 4:3.

사용: PC_AGENT_DIR=docs/_agent/dashun_wang python pipeline/dashun_timeline.py [out.png]
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch
import numpy as np

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
AGENT_DIR = Path(os.environ.get("PC_AGENT_DIR", str(PIPE.parent / "docs" / "_agent" / "dashun_wang")))
LEDGER = AGENT_DIR / "curriculum.json"

INK, SUB, GRID = "#1F2933", "#7B8794", "#E4E7EB"
Y0, Y1 = 2011, 2026
XL, XR = 0.150, 0.910
ORIG = (0.018, 0.132)


def set_korean_font():
    for c in ["/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
              "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]:
        if os.path.exists(c):
            try:
                fm.fontManager.addfont(c)
                plt.rcParams["font.family"] = fm.FontProperties(fname=c).get_name()
                break
            except Exception:
                pass
    plt.rcParams["axes.unicode_minus"] = False


def X(year):
    year = max(Y0, min(Y1, year))
    return XL + (year - Y0) / (Y1 - Y0) * (XR - XL)


# 스트림: (키, 라벨, 색, 소속 강)
STREAMS = [
    ("A", "인간 동역학 · 네트워크 · 시각화", "#159B8A", [1, 11]),
    ("B", "과학적 영향력의 정량화 (인용 동역학)", "#2C6FB3", [2]),
    ("E", "과학의 과학 · 오픈 인프라", "#6C4AA6", [3]),
    ("C", "경력 궤적 · 실패 · 핫스트릭", "#E07B39", [4, 5]),
    ("D", "팀 규모 · 협업 · 파괴적 혁신", "#C0392B", [6]),
    ("G", "창의성 · 아이디어의 확산", "#C64B8C", [7]),
    ("F", "과학 · 정책의 공진화 · 지정학", "#C79A10", [8, 9]),
    ("H", "AI · LLM과 발견의 미래", "#2B3A67", [10]),
]

# 주요 다리(bridge) 논문 — 짧은 표기(slug prefix → label)
BRIDGE_LABEL = {
    "9065_": "Q-모델·랜덤임팩트 (2016)",
    "9043_": "창의성: 영감 vs 준비 (2016)",
    "9086_": "핫스트릭의 발생 (2021)",
    "1003_": "WSB Q-모델 (2013)",
    "9012_": "물리학 100년 (2015)",
    "9013_": "노벨상 데이터셋 (2019)",
    "9081_": "과학의 시간 차원 (2017)",
    "9073_": "지적 계보 (2022)",
    "1023_": "SciSciNet (2023)",
}


def build(out_path):
    set_korean_font()
    led = json.loads(LEDGER.read_text(encoding="utf-8"))
    course = led.get("course", "Dashun Wang 커리큘럼")
    lec2stream = {l: s[0] for s in STREAMS for l in s[3]}

    slug_lecs = defaultdict(set)
    year, title = {}, {}
    for L in led["lectures"]:
        for p in L["papers"]:
            slug_lecs[p["slug"]].add(L["lecture"])
            year[p["slug"]] = p.get("year")
            title[p["slug"]] = p["title"]
    slugs = [s for s in slug_lecs if year.get(s)]
    streams_of = {s: {lec2stream[l] for l in slug_lecs[s]} for s in slugs}

    n = len(STREAMS)
    lane_top, lane_bot = 0.800, 0.150
    order = [s[0] for s in STREAMS]
    lane_idx = {k: i for i, k in enumerate(order)}
    lane_y = {k: lane_top - i * (lane_top - lane_bot) / (n - 1) for i, k in enumerate(order)}
    orig_y = {k: 0.660 - i * (0.660 - 0.340) / (n - 1) for i, k in enumerate(order)}
    count = {k: sum(1 for s in slugs if k in streams_of[s]) for k in order}

    # 교차(다른 주제) 연결 엣지 + 교차 차수
    conns = ald_all_connections()
    cur = set(slugs)
    edges = set()
    for s in cur:
        for e in conns.get(s, []):
            rs = e.get("slug")
            if rs in cur and rs != s and not (streams_of[s] & streams_of[rs]):
                edges.add(tuple(sorted((s, rs))))
    xdeg = defaultdict(int)
    for a, b in edges:
        xdeg[a] += 1
        xdeg[b] += 1
    top_bridges = sorted(cur, key=lambda s: (len(_others(streams_of[s], edges, s, streams_of)),
                                             xdeg[s]), reverse=True)
    topset = set(top_bridges[:9])

    # 노드 좌표: y=속한 주제 레인 평균, 같은(주제,연도)면 x 살짝 분산
    node = {}
    grp = defaultdict(list)
    for s in slugs:
        key = (min(lane_idx[k] for k in streams_of[s]), year[s])
        grp[key].append(s)
    for key, members in grp.items():
        members.sort()
        k = len(members)
        for i, s in enumerate(members):
            dx = (i - (k - 1) / 2) * 0.007
            yy = np.mean([lane_y[k2] for k2 in streams_of[s]])
            node[s] = (X(year[s]) + dx, yy)

    fig = plt.figure(figsize=(13.2, 9.9), dpi=170)       # 4:3
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, facecolor="#FFFFFF", zorder=0))

    ax.text(0.018, 0.965, "%s — 연구 흐름 타임라인" % course,
            fontsize=19, fontweight="bold", color=INK, va="center")
    ax.text(0.018, 0.930,
            "8개 주제의 시간순 흐름과 주제를 잇는 공유·연결 논문 %d편 · 관계 %d개 · %d–%d"
            % (len(cur), len(edges), Y0, Y1),
            fontsize=11, color=SUB, va="center")

    # 연도축 + 그리드
    yaxis = 0.878
    ax.plot([XL, XR], [yaxis, yaxis], color=GRID, lw=1.2, zorder=1)
    for yr in [2011, 2013, 2015, 2017, 2019, 2021, 2023, 2025, 2026]:
        x = X(yr)
        ax.plot([x, x], [0.092, yaxis], color=GRID, lw=0.8, zorder=1)
        ax.text(x, yaxis + 0.010, str(yr), fontsize=9.5, color=SUB, ha="center", va="bottom")
    ax.text(ORIG[1] - 0.006, yaxis + 0.010, "기반 ≤2010", fontsize=9.5, color=SUB,
            ha="right", va="bottom", style="italic")

    # 좌측 기반 블록
    ax.add_patch(FancyBboxPatch((ORIG[0], 0.300), ORIG[1] - ORIG[0], 0.400,
                 boxstyle="round,pad=0.004,rounding_size=0.02", fc="#2F3B52", ec="none", zorder=2))
    ax.text((ORIG[0] + ORIG[1]) / 2, 0.500, "네트워크 과학\n·\n인간 동역학\n기반\n(~2010)",
            fontsize=10.5, color="#FFFFFF", ha="center", va="center", fontweight="bold",
            linespacing=1.5, zorder=3)

    # 스트림 밴드
    color = {s[0]: s[2] for s in STREAMS}
    xs = np.linspace(ORIG[1], XR, 260)
    for key, label, col, lecs in STREAMS:
        yrs = [year[s] for s in slugs if key in streams_of[s]]
        start = min(yrs)
        ly, oy = lane_y[key], orig_y[key]
        w = 0.020 + count[key] * 0.0050
        x0, x1 = ORIG[1], X(start)
        t = np.clip((xs - x0) / max(1e-6, (x1 - x0)), 0, 1)
        s_ = t * t * (3 - 2 * t)
        yc = oy + (ly - oy) * s_
        hw = 0.006 + (w / 2 - 0.006) * s_
        ax.fill_between(xs, yc - hw, yc + hw, color=col, alpha=0.42, lw=0, zorder=3)
        lx = 0.315 if key == "A" else X(start) + 0.012
        ax.text(lx, ly + w / 2 + 0.012, label, fontsize=10.0, fontweight="bold",
                color=col, va="bottom", ha="left", zorder=6)

    # 여러 주제 공유 논문 → 각 레인으로 잇는 얇은 세로 연결
    for s in slugs:
        if len(streams_of[s]) > 1:
            x, y = node[s]
            for k in streams_of[s]:
                ax.plot([x, x], [y, lane_y[k]], color="#333", lw=0.8, alpha=0.5, zorder=5)

    # 주제 간 공유·연결 논문 관계(arc)
    for a, b in edges:
        (x1, y1), (x2, y2) = node[a], node[b]
        ia = min(lane_idx[k] for k in streams_of[a])
        ib = min(lane_idx[k] for k in streams_of[b])
        rad = 0.16 if ia < ib else -0.16
        emph = a in topset or b in topset
        col = "#B4470B" if emph else "#5B6570"
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle="-", color=col,
                                    lw=(1.15 if emph else 0.65),
                                    alpha=(0.42 if emph else 0.20),
                                    connectionstyle="arc3,rad=%.2f" % rad),
                    zorder=(6 if emph else 4))

    # 노드
    for s in slugs:
        x, y = node[s]
        c = color[min(streams_of[s], key=lambda k: lane_idx[k])]
        sz = 32 + min(xdeg[s], 10) * 20
        if xdeg[s] >= 4:
            ax.scatter([x], [y], s=sz + 95, facecolor="none", edgecolors="#2B2F36",
                       linewidths=1.1, zorder=7)
        ax.scatter([x], [y], s=sz, facecolor=c, edgecolors="white", linewidths=1.2, zorder=8)

    # 다리(bridge) 논문 라벨
    placed = []
    for i, s in enumerate(top_bridges[:8]):
        pre = s.split("_")[0] + "_"
        lab = BRIDGE_LABEL.get(pre)
        if not lab:
            continue
        x, y = node[s]
        up = 1 if (i % 2 == 0) else -1
        ty = y + up * 0.052
        ax.plot([x, x], [y, ty], color="#2B2F36", lw=0.7, alpha=0.6, zorder=9)
        ax.text(x, ty + (0.004 if up > 0 else -0.004), lab, fontsize=8.0, color=INK,
                ha="center", va=("bottom" if up > 0 else "top"), zorder=10,
                bbox=dict(boxstyle="round,pad=0.3", fc="#FFFDF6", ec="#2B2F36", lw=0.8, alpha=0.95))

    # 우측 프론티어
    ax.annotate("", xy=(0.935, 0.155), xytext=(0.935, 0.800),
                arrowprops=dict(arrowstyle="-", color="#2B3A67", lw=1.6), zorder=2)
    ax.text(0.943, 0.48, "2024–2026\nAI · 인간 공진화", fontsize=9.4, color="#2B3A67",
            rotation=90, ha="left", va="center", fontweight="bold", linespacing=1.3, zorder=2)

    # 범례 / 캡션
    ax.text(0.018, 0.058,
            "● 노드 = 논문(발표연도) · 노드 클수록 여러 주제와 연결 · "
            "곡선 = 서로 다른 주제를 잇는 공유·연결 논문 관계(주황=핵심 다리 논문)",
            fontsize=9, color=SUB, va="center")
    ax.text(0.018, 0.030, "세로 연결선 = 한 논문이 두 주제에 동시에 속함 (공유 논문)",
            fontsize=9, color=SUB, va="center")
    ax.text(0.018, 0.006, "Dashun Wang 연구 흐름 따라잡기 · 정리편 · Paper Curation",
            fontsize=8.3, color="#B0B6BD", va="center")

    fig.savefig(out_path, facecolor="white")
    print("saved", out_path, os.path.getsize(out_path), "bytes",
          "| papers=%d edges=%d" % (len(cur), len(edges)))
    return out_path


def _others(sset, edges, s, streams_of):
    """s 가 실제로 연결하는 '다른 주제' 집합."""
    out = set()
    for a, b in edges:
        if a == s:
            out |= streams_of[b]
        elif b == s:
            out |= streams_of[a]
    return out - sset


_CONN = None
def ald_all_connections():
    global _CONN
    if _CONN is None:
        import agent_lecture_digest as ald
        _CONN = ald.all_connections()
    return _CONN


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(AGENT_DIR / "dashun_timeline.png")
    build(out)
