"""Todo 23 — public artifact hygiene tests (RED first).

Scan generated public artifacts (HTML/JSON/MD under docs/) for credential
shapes, local absolute paths, and operator emails. No secret may survive
into a public artifact; a synthetic canary must be detected and rejected.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = [
    re.compile("sk-" + "ant-" + r"[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"re_[0-9a-fA-F]{20,}"),
    re.compile(r"(?i)api[_-]?key[\"'\s:=]+[A-Za-z0-9_\-]{16,}"),
]
LOCAL_PATH_PATTERNS = [
    re.compile(r"[A-Za-z]:\\[^\s\"'<>|]{4,}"),
    re.compile(r"/Users/[^\s\"'<>|]+"),
    re.compile(r"/home/[^\s\"'<>|]+"),
]
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scan_artifact(text: str) -> list[str]:
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            matched = match.group(0)
            if "your" in matched.lower() or "example" in matched.lower():
                continue
            findings.append(f"secret-shape: {pattern.pattern[:30]}")
    for pattern in LOCAL_PATH_PATTERNS:
        match = pattern.search(text)
        if match and not any(marker in match.group(0) for marker in ("<", ">", "…", "...")):
            findings.append(f"local-path: {match.group(0)[:40]}")
    for match in EMAIL_PATTERN.finditer(text):
        email = match.group(0)
        if email.endswith(("@example.com", "@example.org", "@localhost")):
            continue
        if "your-domain" in email or email.startswith(("you@", "your@")):
            continue
        findings.append(f"operator-email: {email}")
    return findings


class PublicArtifactScanTests(unittest.TestCase):
    def test_clean_artifact_passes(self) -> None:
        text = "<html><body>Paper review for 001_Alpha. See <a href='/papers/001_Alpha/'>here</a>.</body></html>"
        self.assertEqual(scan_artifact(text), [])

    def test_synthetic_key_canary_detected(self) -> None:
        text = "api key: " + "sk-" + "ant-" + "A" * 52
        self.assertTrue(any("secret-shape" in f for f in scan_artifact(text)))

    def test_local_windows_path_detected(self) -> None:
        text = "generated from C:\\Users\\someone\\paper-curation\\docs"
        self.assertTrue(any("local-path" in f for f in scan_artifact(text)))

    def test_operator_email_detected(self) -> None:
        text = "contact: operator@gmail.com"
        self.assertTrue(any("operator-email" in f for f in scan_artifact(text)))

    def test_example_email_allowed(self) -> None:
        text = "contact: your.email@example.com"
        self.assertFalse(any("operator-email" in f for f in scan_artifact(text)))

    def test_deployed_docs_are_clean(self) -> None:
        """The tracked public docs must contain no secret/local-path shapes."""
        targets = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "README.en.md",
            REPO_ROOT / "docs/setup-guide.md",
        ]
        for path in targets:
            if not path.is_file():
                continue
            findings = scan_artifact(path.read_text(encoding="utf-8"))
            self.assertEqual(findings, [], f"{path}: {findings}")


if __name__ == "__main__":
    unittest.main()
