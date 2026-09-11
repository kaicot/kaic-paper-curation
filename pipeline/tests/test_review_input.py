"""Evidence selection and review-generation provenance regressions."""

from __future__ import annotations

import hashlib
import unittest

from pipeline.lib.review_input import (
    build_review_source,
    model_fingerprint_from_identity,
    review_generation_currentness,
)


class ReviewInputTests(unittest.TestCase):
    def test_section_aware_input_keeps_core_evidence_and_records_truncation(self) -> None:
        text = """# Abstract
ABSTRACT_TOKEN """ + "a" * 900 + """
# 1. Methods
METHOD_TOKEN """ + "m" * 2_000 + """
# 2. Results
RESULT_TOKEN """ + "r" * 2_000 + """
# 3. Discussion
DISCUSSION_TOKEN """ + "d" * 2_000 + """
# 4. Conclusions
CONCLUSION_TOKEN """ + "c" * 2_000

        source, metadata = build_review_source(text, max_characters=3_200)

        self.assertLessEqual(len(source), 3_200)
        self.assertEqual(metadata["selection_mode"], "section_aware")
        self.assertEqual(
            metadata["missing_sections"],
            [],
        )
        self.assertEqual(
            metadata["selected_sections"],
            ["abstract", "methods", "results", "discussion", "conclusion"],
        )
        for token in ("METHOD_TOKEN", "RESULT_TOKEN", "DISCUSSION_TOKEN", "CONCLUSION_TOKEN"):
            self.assertIn(token, source)
        self.assertTrue(metadata["truncated_sections"])

    def test_korean_combined_heading_covers_results_and_discussion(self) -> None:
        text = """초록
요약 근거
1. 연구 방법
METHOD_KO
2. 결과 및 고찰
RESULT_DISCUSSION_KO
3. 결론
CONCLUSION_KO
"""
        source, metadata = build_review_source(text, max_characters=1_000)

        self.assertEqual(metadata["selection_mode"], "full_document")
        self.assertEqual(metadata["missing_sections"], [])
        self.assertIn("results", metadata["selected_sections"])
        self.assertIn("discussion", metadata["selected_sections"])
        self.assertIn("RESULT_DISCUSSION_KO", source)

    def test_plain_pdf_style_english_headings_are_detected(self) -> None:
        text = """Abstract
ABSTRACT_EVIDENCE
Materials and Methods
METHOD_EVIDENCE
Results and Discussion
RESULT_DISCUSSION_EVIDENCE
Conclusions
CONCLUSION_EVIDENCE
"""
        source, metadata = build_review_source(text, max_characters=1_000)

        self.assertEqual(metadata["selection_mode"], "full_document")
        self.assertEqual(metadata["missing_sections"], [])
        self.assertIn("METHOD_EVIDENCE", source)
        self.assertIn("RESULT_DISCUSSION_EVIDENCE", source)
        self.assertIn("CONCLUSION_EVIDENCE", source)

    def test_common_extracted_pdf_heading_forms_are_detected(self) -> None:
        # These are literal heading styles found in the existing local corpus:
        # ``2 Material and Method`` and ``3 Results and Discussion``.
        text = """Abstract
ABSTRACT_EVIDENCE
1 Introduction
INTRODUCTION_EVIDENCE
2 Material and Method\x20
METHOD_EVIDENCE
3 Results and Discussion
RESULT_DISCUSSION_EVIDENCE
4 Conclusion
CONCLUSION_EVIDENCE
"""
        source, metadata = build_review_source(text, max_characters=1_000)

        self.assertEqual(metadata["selection_mode"], "full_document")
        self.assertEqual(metadata["missing_sections"], [])
        self.assertIn("METHOD_EVIDENCE", source)
        self.assertIn("RESULT_DISCUSSION_EVIDENCE", source)

    def test_unstructured_text_uses_distributed_literal_excerpts(self) -> None:
        text = "BEGIN_TOKEN " + "a" * 1_100 + " MIDDLE_ONE " + "b" * 1_100 + " MIDDLE_TWO " + "c" * 1_100 + " END_TOKEN"
        source, metadata = build_review_source(text, max_characters=1_200)

        self.assertLessEqual(len(source), 1_200)
        self.assertEqual(metadata["selection_mode"], "distributed_fallback")
        self.assertEqual(metadata["missing_sections"], [])
        self.assertEqual(metadata["undetected_section_headings"], ["methods", "results", "discussion", "conclusion"])
        self.assertIn("BEGIN_TOKEN", source)
        self.assertIn("END_TOKEN", source)
        self.assertEqual(len(metadata["fallback_excerpts"]), 2)

    def test_affordable_document_is_returned_verbatim_even_if_headings_are_sparse(self) -> None:
        text = "Front matter\nMETHOD EVIDENCE 100000 2013 2018\nLater limitation paragraph\n"
        source, metadata = build_review_source(text, max_characters=40_000)

        self.assertEqual(source, text)
        self.assertEqual(metadata["selection_mode"], "full_document")
        self.assertEqual(metadata["missing_sections"], [])
        self.assertEqual(metadata["undetected_section_headings"], ["methods", "results", "discussion", "conclusion"])

    def test_tiny_section_aware_budget_never_exceeds_its_hard_bound(self) -> None:
        text = """# Methods
METHOD_TOKEN """ + "m" * 600 + """
# Results
RESULT_TOKEN """ + "r" * 600 + """
# Discussion
DISCUSSION_TOKEN """ + "d" * 600 + """
# Conclusion
CONCLUSION_TOKEN """ + "c" * 600
        source, metadata = build_review_source(text, max_characters=256)

        self.assertLessEqual(len(source), 256)
        self.assertEqual(metadata["selection_mode"], "section_aware")

    def test_unclassified_remainder_uses_spare_budget_and_reaches_late_evidence(self) -> None:
        text = """# Methods
METHOD_TOKEN
# Results
RESULT_TOKEN
# Discussion
DISCUSSION_TOKEN
# Conclusion
CONCLUSION_TOKEN
""" + "UNCLASSIFIED_BODY " * 5_000 + "LATE_EVIDENCE_TOKEN"
        source, metadata = build_review_source(text, max_characters=4_000)

        self.assertLessEqual(len(source), 4_000)
        self.assertGreater(len(source), 3_500)
        self.assertIn("LATE_EVIDENCE_TOKEN", source)
        self.assertTrue(metadata["fallback_excerpts"])

    def test_currentness_reports_only_the_changed_dimension(self) -> None:
        source = b"review source"
        _, input_record = build_review_source(source.decode(), max_characters=256)
        identity = {"role": "review", "model": "gpt-6-astra", "reasoning_effort": "high"}
        provenance = {
            "prompt_version": "review-prompt-v2",
            "model_fingerprint": model_fingerprint_from_identity(identity),
            "routing_fingerprint": "a" * 64,
            "review_sha256": hashlib.sha256(b"published review").hexdigest(),
            "review_input": input_record,
        }

        current = review_generation_currentness(
            provenance,
            source,
            b"published review",
            prompt_version="review-prompt-v2",
            model_fingerprint=model_fingerprint_from_identity(identity),
            routing_fingerprint="a" * 64,
        )
        stale = review_generation_currentness(
            provenance,
            b"new source",
            b"different review",
            prompt_version="review-prompt-v3",
            model_fingerprint="b" * 64,
            routing_fingerprint="b" * 64,
        )

        self.assertTrue(current["current"])
        self.assertEqual(
            stale["reasons"],
            ["review-artifact-changed", "source-changed", "prompt-changed", "model-routing-changed", "configured-routing-changed"],
        )


if __name__ == "__main__":
    unittest.main()
