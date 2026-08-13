from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_ROOT / "pipeline"
for candidate in (PROJECT_ROOT, PIPELINE_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pipeline import build_rss
from pipeline import build_topic_index
from pipeline import generate_moc
from pipeline import review_to_html


TOPIC = "qa_fixture"
SLUG = "001_Alpha"
SECRET_CANARIES = (
    "sk-test-secret-canary",
    "paid-key-secret-canary",
    "provider-base-url-canary.invalid",
)
PAID_ENVIRONMENT_KEYS = (
    "ANTH" + "ROPIC_API_KEY",
    "OPEN" + "AI_API_KEY",
    "OPEN" + "AI_BASE_URL",
)
FORBIDDEN_GENERATED_TEXT = (
    "/api/embed",
    "/api/audio-email",
    "/api/citedby",
    "localStorage",
    "sessionStorage",
    'type="password"',
    "api." + "anth" + "ropic.com",
    "api." + "open" + "ai.com",
    "generative" + "language.google" + "apis.com",
    "re" + "send.com",
    "workers" + ".dev",
    "Deep " + "Research",
    "Audio " + "Overview",
)


@final
class LocalTopicUiTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] | None = None
    root = Path()
    docs = Path()
    papers = Path()
    topic_dir = Path()
    slug_dir = Path()
    index: list[dict[str, object]] | None = None

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.docs = self.root / "docs"
        self.papers = self.docs / "papers"
        self.topic_dir = self.docs / TOPIC
        self.slug_dir = self.papers / SLUG
        self.slug_dir.mkdir(parents=True)
        self.topic_dir.mkdir(parents=True)
        _ = (self.slug_dir / "figures").mkdir()
        _ = (self.slug_dir / "figures" / "fig1.png").write_bytes(
            b"\x89PNG\r\n\x1a\nfixture"
        )
        self.index = [
            {
                "classifications": {
                    TOPIC: {
                        "all_categories": ["Agents"],
                        "primary_category": "Agents",
                    }
                },
                "date": "2026-08-05",
                "essence": "Alpha agent local evidence",
                "score": 5,
                "slug": SLUG,
                "title": "Alpha & Beta Agent",
                "topics": [TOPIC],
            }
        ]
        _ = (self.papers / "_papers_index.json").write_text(
            json.dumps(self.index, ensure_ascii=False),
            encoding="utf-8",
        )
        _ = (self.slug_dir / "review.md").write_text(
            """# Alpha Agent

> **저자**: Ada Lovelace  |  **날짜**: 2026-08-05

## Essence
Alpha agent local evidence.

![Figure 1](figures/fig1.png)

## Motivation
- **Known**: Local evidence exists.
- **Gap**: Citation integrity is required.
- **Why**: Unsupported claims reduce trust.
- **Approach**: Use local BM25 evidence.

## Achievement
1. One cited local answer.

## How
- Retrieve and cite.

## Originality
- Local-only.

## Limitation & Further Study
- Fixture only.

## Evaluation
- Novelty: 5/5
- Technical Soundness: 5/5
- Significance: 5/5
- Clarity: 5/5
- Overall: 5/5

**총평**: Local citation fixture.
""",
            encoding="utf-8",
        )
        _ = (
            self.topic_dir / "_category_summaries.json"
        ).write_text(
            json.dumps(
                [
                    {
                        "category": "Agents",
                        "count": 1,
                        "description_ko": "로컬 에이전트 연구",
                        "papers": [
                            {
                                "slug": SLUG,
                                "title": "Alpha & Beta Agent",
                            }
                        ],
                        "sub_themes": [],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _ = (
            self.topic_dir / "_paper_connections.json"
        ).write_text(
            "{}",
            encoding="utf-8",
        )
        _ = (
            self.topic_dir / "_timeline_narrative.json"
        ).write_text(
            json.dumps(
                {
                    "category_analyses": {
                        "Agents": {
                            "current_state_summary": "Local evidence",
                            "sub_themes": [],
                        }
                    },
                    "executive_summary_ko": "로컬 연구 개요",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @override
    def tearDown(self) -> None:
        assert self.temporary is not None
        self.temporary.cleanup()

    def _generate_all(self) -> dict[str, bytes]:
        canary_environment = {
            PAID_ENVIRONMENT_KEYS[0]: SECRET_CANARIES[0],
            PAID_ENVIRONMENT_KEYS[1]: SECRET_CANARIES[1],
            PAID_ENVIRONMENT_KEYS[2]: (
                "https://" + SECRET_CANARIES[2]
            ),
        }
        with (
            patch.dict(os.environ, canary_environment, clear=False),
            patch.object(
                build_topic_index,
                "PAPERS_DIR",
                str(self.papers),
            ),
            patch.object(
                build_topic_index,
                "get_topic_dir",
                return_value=self.topic_dir,
            ),
            patch.object(
                review_to_html,
                "PAPERS",
                str(self.papers),
            ),
            patch.object(
                build_rss,
                "PAPERS_DIR",
                str(self.papers),
            ),
            patch.object(
                build_rss,
                "get_topic_dir",
                return_value=self.topic_dir,
            ),
            patch.object(
                generate_moc,
                "PAPERS_DIR",
                str(self.papers),
            ),
            patch.dict(
                review_to_html.__dict__,
                {
                    "_connections_cache": {},
                    "_DEPLOY_TOPICS": None,
                    "_PIDX": None,
                    "_ZOTERO_KEYS": {},
                },
            ),
        ):
            _ = build_topic_index.build_topic_index(TOPIC)
            review_builder = cast(
                Callable[[str, str, str], str],
                review_to_html.convert_review,
            )
            review_document = review_builder(
                str(self.slug_dir / "review.md"),
                TOPIC,
                str(self.slug_dir),
            )
            _ = (self.slug_dir / "index.html").write_text(
                review_document,
                encoding="utf-8",
            )
            rss_builder = cast(
                Callable[[str], str | None],
                build_rss.build_rss,
            )
            self.assertIsNotNone(rss_builder(TOPIC))
            moc_insights = cast(
                Callable[[str, str], dict[str, object]],
                generate_moc.generate_moc_insights,
            )
            _ = moc_insights(
                TOPIC,
                str(self.topic_dir),
            )
            moc_categories = cast(
                Callable[[str, str], None],
                generate_moc.generate_moc_categories,
            )
            moc_categories(
                TOPIC,
                str(self.topic_dir),
            )
        paths = {
            "topic": self.topic_dir / "index.html",
            "review": self.slug_dir / "index.html",
            "rss": self.topic_dir / "feed.xml",
            "moc_insights": self.topic_dir / "MOC_Insights.md",
            "moc_categories": self.topic_dir / "MOC_Categories.md",
        }
        return {name: path.read_bytes() for name, path in paths.items()}

    def test_local_answer_ui_and_retained_artifacts(self) -> None:
        artifacts = self._generate_all()
        topic_html = artifacts["topic"].decode("utf-8")
        review_html = artifacts["review"].decode("utf-8")

        self.assertIn('id="paper-search"', topic_html)
        self.assertIn('class="paper-card"', topic_html)
        self.assertIn(
            f'../papers/{SLUG}/index.html',
            topic_html,
        )
        self.assertIn('id="local-answer-form"', topic_html)
        self.assertIn('id="local-answer-query"', topic_html)
        self.assertIn('id="local-answer-status"', topic_html)
        self.assertIn('id="local-answer-output"', topic_html)
        self.assertIn('<option value="short">', topic_html)
        self.assertIn('<option value="medium">', topic_html)
        self.assertIn('<option value="long">', topic_html)
        self.assertIn('fetch("/api/session"', topic_html)
        self.assertIn('fetch("/api/answer"', topic_html)
        self.assertIn(
            'payload.schema_version !== 1',
            topic_html,
        )
        self.assertIn("local server required", topic_html)
        self.assertIn("[ref:", topic_html)
        self.assertIn("addEventListener", topic_html)

        self.assertIn("Alpha Agent", review_html)
        self.assertIn("figures/fig1.png", review_html)
        self.assertIn("Essence", review_html)
        self.assertIn(SLUG, artifacts["rss"].decode("utf-8"))
        self.assertIn(
            "<updated>2026-08-01T00:00:00Z</updated>",
            artifacts["rss"].decode("utf-8"),
        )
        self.assertIn(
            "moc-insights-v1",
            artifacts["moc_insights"].decode("utf-8"),
        )
        self.assertIn(
            "Alpha &amp; Beta Agent",
            topic_html,
        )
        self.assertIn(
            'data-search="Alpha &amp; Beta Agent Agents',
            topic_html,
        )
        self.assertNotIn("&amp;amp;", topic_html)
        self.assertIn(
            "Alpha & Beta Agent",
            artifacts["moc_categories"].decode("utf-8"),
        )

        emitted = b"\n".join(artifacts.values())
        for canary in SECRET_CANARIES:
            self.assertNotIn(canary.encode(), emitted)
        for forbidden in FORBIDDEN_GENERATED_TEXT:
            self.assertNotIn(forbidden.encode(), emitted)
        self.assertEqual(
            build_topic_index.theme_accent("ai4s"),
            "#D63423",
        )
        self.assertEqual(
            build_topic_index.theme_accent("scisci"),
            "#2374D6",
        )

    def test_topic_output_is_byte_stable(self) -> None:
        first = self._generate_all()
        second = self._generate_all()
        self.assertEqual(first, second)


if __name__ == "__main__":
    _ = unittest.main()
