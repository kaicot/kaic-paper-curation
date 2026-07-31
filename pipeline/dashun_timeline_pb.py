#!/usr/bin/env python3
"""Dashun Wang 연구 흐름 타임라인 — Paper Curation 방식(PaperBanana generate_diagram)으로 생성.

- generate_timelines.py 의 메인 타임라인과 동일한 경로: method_text + caption → PaperBanana.
- 핵심 요구: 여러 주제가 공유하는 '랜드마크 논문'은 점 하나(환승역)로 두고 여러 스트림이
  그 점을 함께 지나가게(interchange) 그린다. 개별 논문 우측 라벨 없음.
- main model 은 gemini-3.1-pro 쿼터 소진 → configs/model_config.yaml 을 임시로 claude-sonnet-5 로
  스왑(끝나면 원복). image model 은 gemini-3.1-flash-image-preview(정상).
- aspect ratio 4:3.
"""
import os
import sys
from pathlib import Path

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
AGENT_DIR = Path(os.environ.get("PC_AGENT_DIR", str(PIPE.parent / "docs" / "_agent" / "dashun_wang")))
# PaperBanana 는 내부에서 cwd 를 바꾸므로 output_path 는 반드시 절대경로여야 한다.
if not AGENT_DIR.is_absolute():
    AGENT_DIR = (Path.cwd() / AGENT_DIR).resolve()
OUT = (AGENT_DIR / "dashun_timeline.png").resolve()

METHOD = r"""## Research Timeline: Dashun Wang's Research Lineage (Science of Science)

Horizontal Sankey-style timeline. The MAJORITY of horizontal width covers 2011–2026 with
labeled year gridlines at 2011, 2013, 2015, 2017, 2019, 2021, 2023, 2025, 2026. Collapse
everything before 2011 into a single narrow "pre-2011 foundations: network science & human
dynamics" zone pinned at the far-left edge (position only, never dominating the axis).

There are 8 thematic STREAMS. Each stream is one saturated consistent color, a smooth flowing
ribbon starting from the left foundations zone and flowing right. Band width qualitatively
reflects field size (not linearly proportional).

### STREAM A: Human Dynamics, Networks & Visualization (2011 -> 2024)
Relative size: MEDIUM. Color: teal.
Key branches:
- "Crisis mobility & information flow from mobile big data" (2011-2014)
- "Human-dynamics ↔ network-science unification" (2012-2013)
- "Complex-network scaling & visual analytics" (2019-2024)
Interaction: SPAWNS the impact-quantification stream; RESPONDS TO in the policy/pandemic stream.

### STREAM B: Quantifying Scientific Impact — Citation Dynamics (2012 -> 2026)
Relative size: LARGE. Color: blue.
Key branches:
- "WSB long-term impact model" (2013)
- "Reinforced-Poisson & atypical/sleeping-beauty dynamics" (2014-2018)
- "Capacity-based breakthrough prediction" (2023-2026)
Interaction: FEEDS INTO careers and teams; ENABLES science-of-science infrastructure.

### STREAM E: Science of Science & Open Infrastructure (2015 -> 2023)
Relative size: LARGE. Color: purple.
Key branches:
- "A century of physics / census of physics" (2015-2019)
- "The Science of Science (field consolidation)" (2021)
- "SciSciNet open data lake & empirical methods" (2023)
Interaction: The backbone — ENABLES every other stream (shared data & methods).

### STREAM C: Careers, Failure & Hot Streaks (2014 -> 2026)
Relative size: LARGE. Color: orange.
Key branches:
- "Geographic mobility & institutional stratification" (2014-2017)
- "Failure dynamics & early-career setback (near-miss paradox)" (2019-2022)
- "Hot streaks & the pivot penalty" (2021-2026)
Interaction: MERGES WITH teams; FED BY impact models.

### STREAM D: Team Size, Collaboration & Disruption (2019 -> 2026)
Relative size: MEDIUM. Color: red.
Key branches:
- "Large teams develop, small teams disrupt (CD-index)" (2019)
- "Nobel-laureate productivity & the elite revisited" (2019-2020)
- "Interdisciplinary impact via disciplinary grants" (2026)
Interaction: MERGES WITH careers; FED BY impact & science-of-science.

### STREAM G: Creativity & Diffusion of Ideas (2014 -> 2025)
Relative size: SMALL. Color: magenta.
Key branches:
- "Herding effects in crowd wisdom" (2014)
- "Inspiration vs preparation; intellectual lineage" (2016-2022)
- "Peer review & the diffusion of ideas" (2025)
Interaction: FEEDS impact & science-of-science.

### STREAM F: Science–Policy Coevolution & Geopolitics (2020 -> 2026)
Relative size: MEDIUM. Color: gold.
Key branches:
- "Pandemic policy responses & coevolution of policy & science" (2020-2023)
- "Public use/funding & partisan disparities" (2022-2025)
- "Geopolitics of international collaboration; pivoting under tension" (2026)
Interaction: RESPONDS TO human-dynamics crisis methods; SHARES the pivot paper with careers.

### STREAM H: AI, LLMs & the Future of Discovery (2019 -> 2026)
Relative size: MEDIUM. Color: navy.
Key branches:
- "Citation graphs of AI research" (2019)
- "Quantifying AI use & SciSciGPT domain-grounded collaborator" (2024-2025)
- "Benchmark illusion; machine contributions to discovery" (2026)
Interaction: The convergence point — ABSORBS methods from all streams at the right edge.

### SHARED / INTERCHANGE PAPERS (landmark nodes) — CRITICAL
Certain landmark papers belong to MULTIPLE streams at once. Render each as a SINGLE larger
labeled circular NODE (an interchange "station") placed at its year, and route ALL the listed
streams so they physically PASS THROUGH / graze that one node. Do NOT duplicate the paper per
stream; the whole point is that different-colored streams converge on the SAME single point.
Draw short colored connectors from each listed stream's ribbon into the node so the viewer can
tell which topics share it.
- NODE "WSB Q-model / long-term impact" (2013) — passed through by streams: B, A, D, E, G
- NODE "Q-model & random-impact rule" (2016) — passed through by: B, C, D, E, G
- NODE "Creativity: inspiration vs preparation" (2016) — passed through by: G, A, B, C, E, F
- NODE "A century of physics" (2015) — passed through by: E, A, B, D, H
- NODE "Hot streaks onset" (2021) — passed through by: C, B, D, E, G
- NODE "The Science of Science (book)" (2021) — passed through by: E, D, G, H
- NODE "SciSciNet open data lake" (2023) — passed through by: E, C, F, H
- NODE "InnovationInsights visual analytics" (2024) — passed through by: A, B, D, E, H

### BAND WIDTH GUIDE (largest to smallest)
E (Science of Science) ≈ B (Impact) ≈ C (Careers) > D (Teams) ≈ H (AI) ≈ F (Policy) ≈ A (Human dynamics) > G (Creativity)

### CROSS-CUTTING THREADS
- A single mathematical mindset — find one equation under seemingly unrelated phenomena — runs
  from human dynamics (A) through impact models (B) to careers (C) and AI (H).
- The interchange nodes above are the connective tissue: impact/creativity/hot-streak/physics
  landmark papers are shared hubs where multiple topics meet.
- Description -> causal inference -> prediction & intervention: methodological center of gravity
  shifts left-to-right over time.

### TURNING POINTS (annotation boxes — TOP 6, small rounded callouts)
- **2011: Crisis mobility from mobile big data** — human behavior under perturbation.
- **2013: WSB Q-model** — long-term citation impact becomes predictable.
- **2019: Small teams disrupt (CD-index)** — team size reshapes innovation.
- **2021: "The Science of Science"** — the field consolidates.
- **2023: SciSciNet** — open data lake democratizes the field.
- **2026: AI as co-author of discovery** — benchmark illusion & machine credit.

### EMERGENCE & DECLINE EVENTS
- ▶ Team-disruption stream emerges (2019)
- ▶ Policy/geopolitics stream emerges (2020)
- ▶ AI/LLM stream accelerates (2024)

### ABSOLUTE VISUAL RULES
- Horizontal timeline, left to right, years at top; 4:3 aspect ratio.
- Streams as smooth flowing ribbon/river streams with organic curves; NO simple parallel bands.
- Streams MUST interact: merging, branching, and CONVERGING ON SHARED INTERCHANGE NODES.
- Shared/landmark papers = single labeled circular interchange nodes that multiple colored
  streams pass through (this is the most important requirement).
- Bold stream-name labels at each stream's left origin. Milestone annotations as small rounded
  callout boxes. One saturated consistent color per stream. Flat editorial vector look.
- White background, clean sans-serif font.
- NO title in image, NO watermarks, NO color-name text, NO specific paper counts, English only.
"""

CAPTION = (
    "A 4:3 Sankey-style research-lineage timeline of Dashun Wang's work in the Science of Science "
    "(2011–2026). Eight colored thematic streams flow left-to-right from a pre-2011 network-science / "
    "human-dynamics foundation. Landmark papers shared by several topics (e.g. the WSB and random-impact "
    "Q-models, hot streaks, creativity, a century of physics, The Science of Science, SciSciNet, "
    "InnovationInsights) are drawn as SINGLE interchange nodes that multiple streams pass through, making "
    "cross-topic overlap explicit. Milestone callouts mark turning points; the AI/LLM stream converges the "
    "field at the right edge."
)


def main():
    from lib.paperbanana import generate_diagram
    print("[paperbanana] generating Dashun Wang timeline (4:3) ...", flush=True)
    png = generate_diagram(
        method=METHOD, caption=CAPTION,
        aspect_ratio="4:3", critic_rounds=2,
        exp_mode="demo_planner_critic", retrieval_setting="none",
        output_path=str(OUT),
    )
    if not png:
        print("FAILED: no image", flush=True)
        return 1
    print(f"saved {OUT} ({len(png)} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
