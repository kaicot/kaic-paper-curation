"""
Focused, read-only / scratch-only tests for pipeline/auto_recover.py (T1-1).

Covers:
  - high bucket slugs are AUTO-confirmed
  - medium bucket: LLM judge (stubbed) confirms only true-mismatch verdicts
  - --no-llm leaves medium UNCONFIRMED (conservative)
  - missing ANTHROPIC_API_KEY leaves medium UNCONFIRMED
  - LLM judge error → graceful fallback (medium unconfirmed, no crash)
  - command builders emit the correct fix/re-review/audit strings
  - dry-run loop runs ZERO deletions and issues ONLY the audit command

Never runs the real heavy audit and never executes any deletion.
Run with the py312 interpreter:
  PYTHONUTF8=1 /opt/homebrew/Caskroom/miniconda/base/envs/py312/bin/python \
      pipeline/tests/test_auto_recover.py
"""

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE))

import auto_recover as ar  # noqa: E402


# ── Synthetic audit report (high / medium / clean buckets) ───────────────────

def make_report():
    return {
        "topic": "testtopic",
        "total": 5,
        "buckets": {"high": 2, "medium": 2, "clean": 1, "skipped": 0},
        "results": [
            {
                "slug": "003_HighDup",
                "title": "A High Confidence Duplicate Paper",
                "doi": "",
                "confidence": "high",
                "text_md_duplicate": {"hash": "abc123", "peers": ["004_HighFail"]},
                "checks": {
                    "title": {"passed": False, "reason": "title_missing", "coverage": 0.1},
                    "doi": {"passed": None, "reason": "no_doi_in_index", "coverage": 0.0},
                    "arxiv": {"passed": None, "reason": "no_arxiv_in_entry", "coverage": 0.0},
                    "review": {"passed": False, "reason": "review_title_missing", "coverage": 0.0},
                },
            },
            {
                "slug": "004_HighFail",
                "title": "Another High Confidence Mismatch",
                "doi": "10.1234/xyz",
                "confidence": "high",
                "checks": {
                    "title": {"passed": False, "reason": "title_missing", "coverage": 0.2},
                    "doi": {"passed": False, "reason": "doi_absent", "coverage": 0.0},
                    "arxiv": {"passed": None, "reason": "no_arxiv_in_entry", "coverage": 0.0},
                    "review": {"passed": False, "reason": "review_title_missing", "coverage": 0.1},
                },
            },
            {
                "slug": "200_MediumMismatch",
                "title": "A Medium Suspect That Is Truly Wrong",
                "doi": "",
                "confidence": "medium",
                "checks": {
                    "title": {"passed": False, "reason": "title_missing", "coverage": 0.3},
                    "doi": {"passed": None, "reason": "no_doi_in_index", "coverage": 0.0},
                    "arxiv": {"passed": None, "reason": "no_arxiv_in_entry", "coverage": 0.0},
                    "review": {"passed": True, "reason": "review_ok", "coverage": 0.9},
                },
            },
            {
                "slug": "201_MediumClean",
                "title": "A Medium Suspect That Is Actually Fine",
                "doi": "10.5555/ok",
                "confidence": "medium",
                "checks": {
                    "title": {"passed": True, "reason": "title_ok", "coverage": 0.9},
                    "doi": {"passed": False, "reason": "doi_absent", "coverage": 0.0},
                    "arxiv": {"passed": None, "reason": "no_arxiv_in_entry", "coverage": 0.0},
                    "review": {"passed": True, "reason": "review_ok", "coverage": 0.95},
                },
            },
            {
                "slug": "005_Clean",
                "title": "A Perfectly Clean Paper",
                "doi": "10.9999/clean",
                "confidence": "clean",
                "checks": {
                    "title": {"passed": True, "reason": "title_ok", "coverage": 1.0},
                    "doi": {"passed": True, "reason": "doi_ok", "coverage": 1.0},
                    "arxiv": {"passed": None, "reason": "no_arxiv_in_entry", "coverage": 0.0},
                    "review": {"passed": True, "reason": "review_ok", "coverage": 1.0},
                },
            },
        ],
    }


def stub_judge(medium_results):
    """Deterministic LLM stand-in: only 200_* is a true mismatch."""
    out = []
    for r in medium_results:
        if r["slug"].startswith("200"):
            out.append({"slug": r["slug"], "is_mismatch": True,
                        "confidence": 0.95, "reason_ko": "stub: 본문이 다른 논문"})
        else:
            out.append({"slug": r["slug"], "is_mismatch": False,
                        "confidence": 0.9, "reason_ko": "stub: 정상"})
    return out


def raising_judge(medium_results):
    raise RuntimeError("simulated network failure")


# ── Test cases ───────────────────────────────────────────────────────────────

PASSED = []
FAILED = []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def test_high_auto_confirmed():
    print("\n# high bucket slugs are auto-confirmed (with LLM available)")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=stub_judge,
                         has_anthropic_key=True)
    check("high list == {003,004}", set(plan["high"]) == {"003_HighDup", "004_HighFail"})
    check("003 in confirmed", "003_HighDup" in plan["confirmed_slugs"])
    check("004 in confirmed", "004_HighFail" in plan["confirmed_slugs"])
    # high reason mentions duplicate / failure
    reasons = {c["slug"]: c["reason_ko"] for c in plan["confirmed"]}
    check("003 reason mentions SHA dup", "SHA" in reasons["003_HighDup"])
    check("004 reason mentions failure", "검사 실패" in reasons["004_HighFail"])


def test_medium_llm_confirm_and_reject():
    print("\n# medium bucket: stubbed LLM confirms only true-mismatch")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=stub_judge,
                         has_anthropic_key=True)
    check("llm_used True", plan["llm_used"] is True)
    check("200 confirmed", "200_MediumMismatch" in plan["confirmed_slugs"])
    check("201 NOT confirmed", "201_MediumClean" not in plan["confirmed_slugs"])
    check("200 in medium_confirmed", plan["medium_confirmed"] == ["200_MediumMismatch"])
    check("201 in medium_rejected", plan["medium_rejected"] == ["201_MediumClean"])
    check("confirmed total == {003,004,200}",
          set(plan["confirmed_slugs"]) == {"003_HighDup", "004_HighFail", "200_MediumMismatch"})
    # carries through the LLM-provided Korean reason
    med = [c for c in plan["confirmed"] if c["slug"] == "200_MediumMismatch"][0]
    check("200 reason from LLM", med["reason_ko"] == "stub: 본문이 다른 논문")


def test_no_llm_leaves_medium_unconfirmed():
    print("\n# --no-llm leaves medium UNCONFIRMED")
    plan = ar.build_plan(make_report(), no_llm=True, llm_judge=stub_judge,
                         has_anthropic_key=True)
    check("llm_used False", plan["llm_used"] is False)
    check("confirmed == only high {003,004}",
          set(plan["confirmed_slugs"]) == {"003_HighDup", "004_HighFail"})
    check("medium_skipped == {200,201}",
          set(plan["medium_skipped"]) == {"200_MediumMismatch", "201_MediumClean"})
    check("no medium confirmed", plan["medium_confirmed"] == [])


def test_no_api_key_leaves_medium_unconfirmed():
    print("\n# missing ANTHROPIC_API_KEY leaves medium UNCONFIRMED")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=stub_judge,
                         has_anthropic_key=False)
    check("llm_used False (no key)", plan["llm_used"] is False)
    check("confirmed == only high",
          set(plan["confirmed_slugs"]) == {"003_HighDup", "004_HighFail"})
    check("medium_skipped == {200,201}",
          set(plan["medium_skipped"]) == {"200_MediumMismatch", "201_MediumClean"})


def test_llm_error_graceful_fallback():
    print("\n# LLM judge error → graceful fallback (no crash, medium unconfirmed)")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=raising_judge,
                         has_anthropic_key=True)
    check("llm_error captured", plan["llm_error"] is not None)
    check("llm_used False on error", plan["llm_used"] is False)
    check("confirmed == only high",
          set(plan["confirmed_slugs"]) == {"003_HighDup", "004_HighFail"})
    check("medium_skipped == {200,201}",
          set(plan["medium_skipped"]) == {"200_MediumMismatch", "201_MediumClean"})


def test_slugs_filter():
    print("\n# --slugs filter restricts the confirmed set")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=stub_judge,
                         has_anthropic_key=True, slugs_filter=["003"])
    check("only 003 confirmed", plan["confirmed_slugs"] == ["003_HighDup"])


def test_command_strings():
    print("\n# command builders emit the correct fix/re-review/audit strings")
    slugs = ["003_HighDup", "004_HighFail", "200_MediumMismatch"]
    fix = ar.fix_command("ai4s", slugs)
    rer = ar.rereview_command("ai4s", slugs)
    aud = ar.audit_command("ai4s")

    check("fix uses fix_matching.py", fix[2].endswith("fix_matching.py"))
    check("fix has --topic ai4s", "--topic" in fix and fix[fix.index("--topic") + 1] == "ai4s")
    check("fix slugs are sorted NNN prefixes",
          fix[fix.index("--slugs") + 1] == "003,004,200")
    check("fix ends with --execute", fix[-1] == "--execute")

    check("re-review uses run_update_force.py", rer[2].endswith("run_update_force.py"))
    check("re-review slugs == 003,004,200",
          rer[rer.index("--slugs") + 1] == "003,004,200")
    check("re-review has --strict-pdf", "--strict-pdf" in rer)
    check("re-review ends with --no-deploy (no unattended deploy/push)",
          rer[-1] == "--no-deploy")

    check("audit uses audit_matching.py", aud[2].endswith("audit_matching.py"))
    check("audit has --topic ai4s", aud[-1] == "ai4s")

    # _prefixes dedups + sorts
    check("_prefixes dedups", ar._prefixes(["003_A", "003_B", "010_C"]) == ["003", "010"])


def test_print_plan_returns_cmds():
    print("\n# print_plan emits the same command strings for the confirmed set")
    plan = ar.build_plan(make_report(), no_llm=False, llm_judge=stub_judge,
                         has_anthropic_key=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmds = ar.print_plan(plan, "ai4s")
    out = buf.getvalue()
    check("print_plan returned cmds", cmds is not None)
    check("fix_cmd ends with --execute", cmds["fix_cmd"][-1] == "--execute")
    check("rereview_cmd has --strict-pdf", "--strict-pdf" in cmds["rereview_cmd"])
    check("rereview_cmd ends with --no-deploy", cmds["rereview_cmd"][-1] == "--no-deploy")
    check("output lists 200 with LLM reason", "stub: 본문이 다른 논문" in out)
    check("output shows fix line", "fix_matching.py" in out)


def test_dry_run_loop_zero_deletions():
    print("\n# dry-run loop: ZERO deletions, ONLY the audit command issued")
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        # write synthetic report where _load_report will look
        (scratch / "_audit_report.json").write_text(
            json.dumps(make_report()), encoding="utf-8")

        # fake some artifacts to prove nothing gets deleted
        artifact = scratch / "fake_review.md"
        artifact.write_text("content", encoding="utf-8")

        # redirect get_topic_dir to the scratch dir
        orig_get_topic_dir = ar.get_topic_dir
        ar.get_topic_dir = lambda t: scratch

        issued = []

        def fake_run_cmd(cmd):
            issued.append(list(cmd))
            return 0  # pretend success; do NOTHING (no deletion, no review)

        try:
            result = ar.run_recover(
                "testtopic", execute=False, max_rounds=2, no_llm=True,
                llm_judge=stub_judge, run_cmd=fake_run_cmd)
        finally:
            ar.get_topic_dir = orig_get_topic_dir

        check("artifact still exists (zero deletions)", artifact.exists())
        check("exactly one subprocess issued (audit only)", len(issued) == 1)
        check("the single issued cmd is the audit",
              bool(issued) and issued[0][2].endswith("audit_matching.py"))
        check("no fix_matching issued",
              not any("fix_matching.py" in c for cmd in issued for c in cmd))
        check("no run_update_force issued",
              not any("run_update_force.py" in c for cmd in issued for c in cmd))
        check("result not converged (mismatches existed, dry-run stopped)",
              result["converged"] is False and result["execute"] is False)
        check("round 1 recorded high=2",
              result["rounds"] and result["rounds"][0]["high"] == 2)


def main():
    test_high_auto_confirmed()
    test_medium_llm_confirm_and_reject()
    test_no_llm_leaves_medium_unconfirmed()
    test_no_api_key_leaves_medium_unconfirmed()
    test_llm_error_graceful_fallback()
    test_slugs_filter()
    test_command_strings()
    test_print_plan_returns_cmds()
    test_dry_run_loop_zero_deletions()

    print(f"\n==== {len(PASSED)} passed, {len(FAILED)} failed ====")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
