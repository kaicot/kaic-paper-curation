#!/usr/bin/env python3
"""12강(정리편 포함) 각각을 html+mp3+연구지도 .zip 으로 묶어 12통의 개별 메일로 발송."""
import io
import sys
import os
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lecture_digest as ald  # noqa: E402
import html as H  # noqa: E402
from _dw_board_pack import ONELINE  # noqa: E402

TO = ["jehyunlee@kist.re.kr"]
LEC = ald.OUTDIR
AD = ald.AGENT_DIR
MAXB = 27 * 1024 * 1024  # 안전선(raw), base64 ~1.37x → ~37MB < Resend 40MB


def zip_of(items):
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    for arc, path in items:
        if path.exists():
            z.writestr(arc, path.read_bytes())
    z.close()
    return buf.getvalue()


def main():
    led = ald.load_ledger()
    titles = {L["lecture"]: L["title"] for L in led["lectures"]}
    plan = []
    for n in range(1, 12):
        tag = f"제{n}강"
        items = [
            (f"{tag}.html", LEC / f"lecture_{n:02d}.html"),
            (f"{tag}_오디오개요.mp3", LEC / f"lecture_{n:02d}.mp3"),
            (f"{tag}_연구지도.png", LEC / f"lecture_{n:02d}_map.png"),
        ]
        plan.append((n, titles[n], ONELINE[n], items))
    # 12번째 = 정리편
    plan.append((12, "정리편 — 11강 총정리", ONELINE[0], [
        ("정리편.html", LEC / "lecture_00_summary.html"),
        ("정리편_오디오개요.mp3", LEC / "lecture_00_summary.mp3"),
        ("전체연구지도.png", AD / "curriculum_map.png"),
        ("연구흐름타임라인.png", AD / "dashun_timeline.png"),
    ]))

    ok = 0
    for i, (n, title, oneline, items) in enumerate(plan, 1):
        data = zip_of(items)
        mb = len(data) / 1048576
        present = [arc for arc, p in items if p.exists()]
        if len(data) >= MAXB:
            print(f"  [{i}/12] ⚠️ {mb:.1f}MB 초과 — 스킵/점검 필요 ({title})")
            continue
        subj = f"[게시판용 {i}/12] {title} (html·음성·연구지도)"
        body = (
            f"<p><b>{H.escape(title)}</b></p>"
            f"<p>{H.escape(oneline)}</p>"
            f"<p>첨부: 강의노트(html) · 오디오개요(mp3) · 연구지도(png) — 압축 {mb:.0f}MB.</p>"
            f"<p style='color:#999;font-size:.85rem'>Dashun Wang 연구 흐름 따라잡기 · {i}/12 · Paper Curation</p>")
        fname = f"{('제%d강' % n) if n <= 11 else '정리편'}_세트.zip"
        st, resp = ald.send_email(TO, subj, body, [(fname, data)])
        good = st == 200
        ok += good
        print(f"  [{i}/12] {mb:5.1f}MB  {'OK ' if good else 'FAIL'} status={st}  {title}  files={present}")
        if not good:
            print("       resp:", str(resp)[:160])
        time.sleep(1.3)  # Resend rate limit 여유
    print(f"DONE: {ok}/12 sent")
    return 0 if ok == 12 else 1


if __name__ == "__main__":
    sys.exit(main())
