# Changelog

All notable project changes are recorded here.

The project follows Semantic Versioning (`MAJOR.MINOR.PATCH`). The project
version is independent from the pinned Codex CLI version.

## [0.1.0] - 2026-08-08

First explicitly versioned local release of the paper-curation pipeline.

### Added

- Codex saved-auth generation through the Terra/Luna gateway, with paid API
  fallback permanently denied.
- Fail-closed setup, doctor, and `run_full` policy boundaries.
- Deterministic BM25-first search and local Deep Research answer serving.
- One-paper fixture coverage for curation, cache identity, resume, and release
  evidence.
- Final-review evidence tooling for command envelopes, negative controls,
  cleanup assertions, and attachment integrity.

### Changed

- Pinned the attested Codex CLI boundary to `0.147.0`.
- Documented the local-only operating model and release verification flow.

### Verification

- Release-gate regression: `238 passed, 0 failed, 0 skipped`.
- F1, F2, F3, and F4 final review gates: PASS.
- F3 saved-auth local canary: PASS.

### Verification boundary

Broad `unittest discover` also collects legacy metrics tests outside the
release gate. They currently report a missing `python-dateutil` environment
dependency and stale pipeline-wiring assertions (3 errors, 1 failure). These
pre-existing issues are not caused by the release diff.
