#!/usr/bin/env python3
"""정리편까지 12강 게시판 패키지: (1) zip 메일, (2) 한줄요약 + (3) 논문리스트 메일."""
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lecture_digest as ald  # noqa: E402
import html as H  # noqa: E402

AD = ald.AGENT_DIR
LEC = ald.OUTDIR

# 12편 한줄요약 (게시판 소개용)
ONELINE = {
    1: "재난 속 휴대폰 빅데이터로 사람들의 이동·소통이 어떻게 요동치는지, 통계물리학이 인간 행동과 네트워크를 잇는 첫 문법을 읽는다.",
    2: "‘언제·얼마나 인용될까’ — WSB Q-모델과 강화 푸아송 과정으로 논문·연구자의 장기 영향력을 예측 가능한 과학으로 만든다.",
    3: "뉴턴의 ‘거인의 어깨’까지 데이터로 검증하는 자기반영적 과학과, 이를 떠받치는 SciSciNet 오픈 인프라를 조망한다.",
    4: "과학·창업·보안을 관통하는 실패의 물리학 — ‘근소한 탈락’이 장기적으로 더 큰 성공을 부르는 역설을 인과적으로 파헤친다.",
    5: "이동·전공 전환(피벗)·연속 대박(핫스트릭) — 과학자 한 사람의 생애를 공간·인지·시간 3차원 궤적으로 해부한다.",
    6: "큰 팀은 발전시키고 작은 팀은 뒤집는다(CD-index) — 노벨상 데이터로 협업 구조와 파괴적 혁신의 관계를 밝힌다.",
    7: "창의성은 영감인가 준비인가, 아이디어는 동료평가·지적 계보를 타고 어떻게 퍼지며 군중의 평점은 왜 한쪽으로 쏠리는가.",
    8: "팬데믹 — 114개국의 정책과 과학이 서로를 끌어당기는 공진화, 그리고 접촉추적망에서 숨은 전파자를 찾는 네트워크 추론.",
    9: "누가 과학을 쓰고 지원하는가 — 공공 활용·자금의 정렬, 당파적 격차, 지정학 긴장 속 국제협력과 전략적 피벗.",
    10: "AI 활용의 실측과 SciSciGPT 협업자, 그리고 ‘Benchmark Illusion’ — 기계의 기여를 어떻게 신뢰하고 인정할 것인가.",
    11: "자아중심 상호작용부터 과학기술의 이중 프론티어까지 — 대체 시스템의 스케일링과 예측 가능성을 시각적으로 읽는다.",
    0: "11강을 두 확장 축(연구대상: 논문→과학자→팀→기관→생태계→AI·인간 / 방법론: 사례→경력→교차도메인→오픈인프라→LLM)으로 되짚고, 연구현장이 챙길 인사이트로 번역한다.",
}


def _slugname(t):
    return re.sub(r"[^가-힣A-Za-z0-9]+", "_", t)[:34].strip("_")


def build_zip(include_mp3: bool) -> tuple[bytes, list[str]]:
    led = ald.load_ledger()
    titles = {L["lecture"]: L["title"] for L in led["lectures"]}
    manifest = []
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    readme = ["Dashun Wang 연구 흐름 따라잡기 — 정리편까지 12강 게시판 패키지", "=" * 54, ""]

    for n in range(1, 12):
        folder = f"제{n:02d}강_{_slugname(titles[n])}"
        html = LEC / f"lecture_{n:02d}.html"
        mp3 = LEC / f"lecture_{n:02d}.mp3"
        mp = LEC / f"lecture_{n:02d}_map.png"
        have = []
        if html.exists():
            z.writestr(f"{folder}/제{n}강.html", html.read_bytes()); have.append("html")
        if include_mp3 and mp3.exists():
            z.writestr(f"{folder}/제{n}강_오디오개요.mp3", mp3.read_bytes()); have.append("mp3")
        if mp.exists():
            z.writestr(f"{folder}/제{n}강_연구지도.png", mp.read_bytes()); have.append("연구지도")
        readme.append(f"제{n:2d}강 · {titles[n]}")
        readme.append(f"   한줄요약: {ONELINE[n]}")
        readme.append(f"   수록: {', '.join(have) if have else '(없음)'}")
        readme.append("")
        manifest.append(f"제{n}강: {', '.join(have) if have else '없음'}")

    # 정리편
    folder = "제12강_정리편_총정리"
    s_html = LEC / "lecture_00_summary.html"
    s_mp3 = LEC / "lecture_00_summary.mp3"
    cmap = AD / "curriculum_map.png"
    tl = AD / "dashun_timeline.png"
    have = []
    if s_html.exists():
        z.writestr(f"{folder}/정리편.html", s_html.read_bytes()); have.append("html")
    if include_mp3 and s_mp3.exists():
        z.writestr(f"{folder}/정리편_오디오개요.mp3", s_mp3.read_bytes()); have.append("mp3")
    if cmap.exists():
        z.writestr(f"{folder}/전체연구지도.png", cmap.read_bytes()); have.append("전체연구지도")
    if tl.exists():
        z.writestr(f"{folder}/연구흐름타임라인.png", tl.read_bytes()); have.append("연구흐름타임라인")
    readme.append(f"제12강(정리편) · 11강 총정리")
    readme.append(f"   한줄요약: {ONELINE[0]}")
    readme.append(f"   수록: {', '.join(have)}")
    readme.append("")
    manifest.append(f"정리편: {', '.join(have)}")

    readme += ["", "[안내]",
               "· 제2~5강은 오디오(mp3)가 빌드되지 않아 미포함(html+연구지도만).",
               "· 제6~11강은 이 PC에 html/mp3 산출물이 없어 연구지도만 포함"
               "(예약발송분이 다른 장비에 있음).",
               "· 오디오 포함 전체본은 용량(약 40MB↑)이라 메일 첨부에서 제외될 수 있음."]
    z.writestr("000_수록안내.txt", "\n".join(readme))
    z.close()
    return buf.getvalue(), manifest


def send_zip():
    data, manifest = build_zip(include_mp3=True)
    full_mb = len(data) / 1048576
    # 메일 첨부 안전선(약 28MB) 초과면 mp3 제외본으로 발송
    if full_mb >= 27:
        data_mail, _ = build_zip(include_mp3=False)
        note_mp3 = (f"<li>⚠️ 오디오 포함 전체본은 {full_mb:.0f}MB로 메일 한도를 초과해, "
                    f"이 메일에는 <b>mp3를 뺀 판({len(data_mail)/1048576:.0f}MB)</b>을 첨부합니다. "
                    f"오디오 포함 전체본은 로컬 <code>docs/_agent/dashun_wang/dashun_강의세트_전체.zip</code> 에 저장.</li>")
        (AD / "dashun_강의세트_전체.zip").write_bytes(data)
        attach = data_mail
    else:
        note_mp3 = f"<li>오디오 포함 전체본 첨부({full_mb:.0f}MB).</li>"
        attach = data
    (AD / "dashun_강의세트_메일.zip").write_bytes(attach)
    body = (
        "<p><b>Dashun Wang 연구 흐름 따라잡기 — 정리편까지 12강 게시판 패키지</b></p>"
        "<p>각 강 폴더에 html · (있으면)오디오개요 mp3 · 연구지도를 담았습니다. 압축 안에 000_수록안내.txt(한줄요약 포함).</p>"
        "<ul>" + "".join(f"<li>{H.escape(m)}</li>" for m in manifest) + "</ul>"
        "<p><b>중요 안내</b></p><ul>"
        "<li>제2~5강 오디오 미빌드(html+연구지도만), 제6~11강은 이 PC에 산출물이 없어 연구지도만 포함"
        "(html/mp3 예약발송분은 다른 장비 보관).</li>"
        + note_mp3 + "</ul>"
        "<p style='color:#999;font-size:.85rem'>Paper Curation 자동 발송</p>")
    st, resp = ald.send_email(["jehyunlee@kist.re.kr"],
                              "[게시판용] Dashun Wang 12강 세트 (html·오디오·연구지도)", body,
                              [("dashun_강의세트.zip", attach)])
    print(f"[ZIP] full={full_mb:.1f}MB attach={len(attach)/1048576:.1f}MB status={st} {resp}")
    return st == 200


def send_summaries_and_list():
    led = ald.load_ledger()
    titles = {L["lecture"]: L["title"] for L in led["lectures"]}
    rows = "".join(
        f"<tr><td style='padding:4px 8px;font-weight:700;white-space:nowrap;vertical-align:top'>제{n}강</td>"
        f"<td style='padding:4px 8px;vertical-align:top'><b>{H.escape(titles[n])}</b><br>"
        f"<span style='color:#444'>{H.escape(ONELINE[n])}</span></td></tr>"
        for n in range(1, 12))
    rows += ("<tr><td style='padding:4px 8px;font-weight:700;white-space:nowrap;vertical-align:top'>제12강<br>(정리편)</td>"
             "<td style='padding:4px 8px;vertical-align:top'><b>11강 총정리</b><br>"
             f"<span style='color:#444'>{H.escape(ONELINE[0])}</span></td></tr>")

    md = (AD / "dashun_paper_list.md").read_text(encoding="utf-8")
    core_n = sum(1 for l in md.splitlines() if l.startswith("- ")) - 62
    body = (
        "<p><b>(2) 12편 게시판 한줄요약</b></p>"
        "<table style='border-collapse:collapse;font-size:14px;line-height:1.5'>" + rows + "</table>"
        "<p style='margin-top:20px'><b>(3) Dashun Wang 논문 · 연관 논문 리스트</b></p>"
        f"<p>형식: <code>{{1저자 et al.}} ({{출판년도}}) {{DOI/URL}} : {{주요내용}}</code> · "
        f"핵심 {core_n}편(강의별) + 연관 62편. 전체 목록은 첨부 <b>dashun_paper_list.md</b> 참고.</p>"
        "<pre style='white-space:pre-wrap;font-size:12px;color:#333;background:#f7f7f9;"
        "border:1px solid #eee;border-radius:8px;padding:10px 12px;max-height:none'>"
        + H.escape(md) + "</pre>"
        "<p style='color:#999;font-size:.85rem'>Paper Curation 자동 발송 · (1) zip은 별도 메일</p>")
    st, resp = ald.send_email(["jehyunlee@kist.re.kr"],
                              "[게시판용] Dashun Wang 12강 한줄요약 + 논문·연관논문 리스트", body,
                              [("dashun_paper_list.md", md.encode("utf-8"))])
    print(f"[SUMMARY+LIST] status={st} {resp}")
    return st == 200


if __name__ == "__main__":
    ok1 = send_zip()
    ok2 = send_summaries_and_list()
    print("DONE zip=", ok1, "summary+list=", ok2)
