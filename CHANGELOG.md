# Changelog

All notable project changes are recorded here.

The project follows Semantic Versioning (`MAJOR.MINOR.PATCH`). The project
version is independent from the pinned Codex CLI version.

## [0.2.0] - 2026-08-13

First real-world usage release: the pipeline was run end-to-end on a real
Zotero collection (36 papers, 19 with local PDFs) and the blockers found
during that run were fixed. New-key feature: LLM-guided ("wizard") setup.

### Added

- **LLM wizard setup flow**: README and AGENTS.md now define a step-by-step
  installation conversation (one question at a time) so a user only needs to
  say "install this repo" and answer prompts: Codex login check, Zotero
  collection name, topic alias (English-only rule), PDF directory, and
  collection-name verification with the available-collections list.
- **Keyless start path**: new read-only `pipeline/tools/inspect_local_zotero.py`
  reads the local `zotero.sqlite` + `storage/` to list collections and report
  how many PDFs are already synced, so users without a Zotero API key (or
  Zotero beginners) can still be guided through setup. Documented in README
  and setup-guide.
- `.gitignore` whitelist entries for `pipeline/tools/`.

### Fixed (real-run blockers)

- Module-import failures when scripts were run from the repo root:
  `sync_zotero.py`, `build_papers_index.py`, `review_to_html.py`,
  `build_rss.py` (package-relative imports, `pipeline._env_guard`).
- `find_pdf()` did not resolve Zotero cloud attachments (`imported_url`,
  `path` empty): now falls back to the child `filename` and the local
  `storage/<childKey>/` layout, which recovered 19 PDFs from a real library.
- Review generation did not emit the `schema_version: v1` frontmatter that
  `validate_default_artifacts()` requires; template fixed and existing
  review.md files back-filled.
- First-run crash when `docs/papers/` did not exist yet (now created).
- `_papers_index.json` stored a truncated 16-char sha256 that the release
  validator rejected as `source-hash-invalid`; now stores the full sha256.
- `prepare_local_models.py --specter2` failed on the current HuggingFace
  adapter layout (no `proximity/` subfolder) and on a stale `.cache` dir;
  adapter-root fallback added.
- Release validators did not match the actual producers: classification
  `assignments` list form, Atom `feed.xml` (RSS vs Atom), and connection
  coverage when small categories (<3 papers) were skipped. Validators now
  accept the real formats and connection generation covers all non-"Other"
  categories.
- `extract_insights.py` only connected categories with >=3 papers; now all
  non-"Other" categories are included.

### Verification

- Real end-to-end run on `고령치매2025_1` collection: 19/36 papers reviewed
  (17 had no local PDF), classification/summary/connections/timeline/HTML/
  BM25/RSS/MOC all generated and `run_full` finished with
  `status: succeeded`.
- Local server verified: topic page, per-paper review pages, BM25 query,
  and the Deep Research answer API all returned 200 with expected content.
- Secret scan (`scripts/scan-secrets.py`) and API-key literal scan over the
  tracked tree: no credentials, no key literals.

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
