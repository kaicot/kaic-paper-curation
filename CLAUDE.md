# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.
It is the Claude-equivalent of `AGENTS.md` — read `AGENTS.md` for the full
instructions. Key rules repeated here:

## Hard rules

- **Never interact with the upstream repo** (`jehyunlee/paper-curation`): no PR,
  issue, push, fetch/merge, or sync. This is the kaicot fork and is operated
  independently (owner instruction 2026-08-13).
- Skill name and invocation is always `kaic-paper-curation`.
- This fork is **local-only**: no Cloudflare deploy, no `wrangler.toml`, no
  `prepare_deploy.py`, no `worker/`, no gh-pages. Results are served by
  `pipeline/serve_local.py` at `http://localhost:8000/{topic}/`.
- Generation uses **Codex saved-auth (ChatGPT login)** only. Paid API fallback
  is permanently denied (`allow_paid_api: false`). `--llm-mode off` runs
  deterministic stages only.

## User experience principle

The user is a beginner who does not type commands. The LLM/agent does
everything: install wizard (one question at a time), daily-use wizard
(request → command mapping), and result URL guidance
(`http://localhost:8000/{topic}/`).

## Key entry points

| Tool | Purpose |
|---|---|
| `pipeline/run_full.py` | Single orchestrator (`--mode/--source/--images`) |
| `pipeline/tools/add_paper_to_zotero.py` | Register a paper from PDF/URL (auto collection) |
| `pipeline/tools/inspect_local_zotero.py` | Read local Zotero collections/PDF state |
| `pipeline/serve_local.py` | Local server (localhost:8000) |
| `pipeline/setup.py` | Setup + skill install (`--install-skill`) |

See `AGENTS.md`, `docs/setup-guide.md`, `docs/operations.md`, and
`docs/architecture.md` for details.
