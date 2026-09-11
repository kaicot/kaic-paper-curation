"""Compare saved-auth review models into a separate, resumable local report."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.lib.review_input import build_review_source
from pipeline.providers.codex_gateway import CodexGateway, CodexGatewayError
from pipeline.run_update_force import build_review_prompt, _review_schema, _validate_review_data


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def prepare_gateway(output: Path, model: str, effort: str) -> CodexGateway:
    workspace = output / "runtime" / model
    for relative in ("pipeline/codex-cli-policy.json", "pipeline/codex-cli-contract.json",
                     "pipeline/model-config.json", "pipeline/schemas/codex-canary-v1.json"):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    path = workspace / "pipeline/model-config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["roles"]["review"].update(model=model, reasoning_effort=effort)
    save(path, config)
    return CodexGateway.production(workspace)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slugs", required=True, help="Comma-separated exact paper directory names")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Run saved-auth generation; default only lists work")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="high")
    args = parser.parse_args()
    papers = (ROOT / "docs/papers").resolve()
    output = args.output_dir.resolve()
    allowed = (ROOT / ".omo").resolve()
    if allowed not in output.parents:
        parser.error("comparison output must be a separate directory beneath .omo")
    slugs = args.slugs.split(",")
    paths = [(papers / slug).resolve() for slug in slugs]
    if not 3 <= len(slugs) <= 5 or len(set(slugs)) != len(slugs):
        parser.error("choose 3 to 5 distinct papers")
    for path in paths:
        if path.parent != papers or not (path / "text.md").is_file():
            parser.error("all slugs must identify an existing paper with extracted text")
    plan = {"schema": "review-comparison-v1", "slugs": slugs,
            "models": ["gpt-5.6-terra", "gpt-6-astra"], "reasoning_effort": args.effort,
            "same_input_and_prompt": True, "originals_modified": False,
            "quality_status": "requires-source-review", "execute": args.execute}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=True)
    index = json.loads((papers / "_papers_index.json").read_text(encoding="utf-8"))
    rows = index if isinstance(index, list) else index.get("papers", [])
    metadata = {row.get("slug"): row for row in rows if isinstance(row, dict)}
    result_rows = []
    for model in plan["models"]:
        gateway = prepare_gateway(output, model, args.effort)
        for path in paths:
            target = output / path.name / (model + ".json")
            source_bytes = (path / "text.md").read_bytes()
            source, selection = build_review_source(source_bytes.decode("utf-8"))
            info = metadata.get(path.name, {})
            prompt = build_review_prompt(title=str(info.get("title", path.name)),
                abstract=str(info.get("abstract", "")), figures=[],
                review_source=source, source_metadata=selection)
            comparison_contract = {"model": model, "effort": args.effort, "prompt": prompt,
                                   "schema": _review_schema(), "cli_contract": gateway.contract,
                                   "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                                   "provider_policy": gateway.policy}
            fingerprint = hashlib.sha256(json.dumps(comparison_contract, sort_keys=True).encode()).hexdigest()
            if target.is_file():
                try:
                    row = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(row, dict) and row.get("fingerprint") == fingerprint and row.get("status") == "ok":
                        value = row.get("review")
                        checksum = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        if isinstance(value, dict) and row.get("review_sha256") == checksum:
                            _validate_review_data(value)
                            result_rows.append(row)
                            continue
                except (OSError, ValueError):
                    pass
            start = time.monotonic()
            row = {"slug": path.name, "model": model, "reasoning_effort": args.effort,
                   "fingerprint": fingerprint, "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                   "input_selection": selection, "status": "failed"}
            try:
                value = gateway.generate_json("review", prompt, _review_schema())
                _validate_review_data(value)
                row.update(status="ok", review=value, schema_and_korean_validation="pass",
                           review_sha256=hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
                           runtime_provenance=getattr(gateway, "last_run_provenance", None))
            except (CodexGatewayError, ValueError) as exc:
                row["error"] = str(exc)
            row["elapsed_seconds"] = round(time.monotonic() - start, 3)
            row["usage"] = getattr(gateway, "last_run_metrics", None)
            save(target, row)
            result_rows.append(row)
            save(output / "report.json", {**plan, "results": result_rows})
            print(json.dumps({k: row[k] for k in ("slug", "model", "status", "elapsed_seconds")}), flush=True)
            if row["status"] != "ok":
                # A failing provider boundary is not permission to retry six times.
                return 2
    save(output / "report.json", {**plan, "results": result_rows,
                                  "completed": sum(row["status"] == "ok" for row in result_rows),
                                  "failed": sum(row["status"] != "ok" for row in result_rows)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
