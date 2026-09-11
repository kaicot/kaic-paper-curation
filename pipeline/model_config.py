"""Validated task routing, independent of the installed Codex release."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, TypeAlias

CodexRole: TypeAlias = Literal[
    "review", "answer", "timeline", "connections", "category_summary", "topic_labels",
    "long_form", "short_form",
]
ROOT = Path(__file__).resolve().parents[1]
TASKS = ("review", "answer", "timeline", "connections", "category_summary", "topic_labels")
ALIASES = {"long_form": "answer", "short_form": "category_summary"}
MODELS = {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def load_model_config(root: Path | None = None) -> dict:
    path = (root or ROOT) / "pipeline/model-config.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("cache_epoch") != "generation-contract-v2":
        raise ValueError("unsupported model configuration schema/cache contract")
    roles = value.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(TASKS):
        raise ValueError("model configuration must define every supported task")
    for role, entry in roles.items():
        if not isinstance(entry, dict) or set(entry) != {"model", "reasoning_effort", "timeout_seconds"}:
            raise ValueError(f"invalid task configuration: {role}")
        if entry["model"] not in MODELS or entry["reasoning_effort"] not in EFFORTS:
            raise ValueError(f"unsupported model or effort: {role}")
        timeout = entry["timeout_seconds"]
        if type(timeout) is not int or not 30 <= timeout <= 1800:
            raise ValueError(f"invalid timeout: {role}")
    return value


def load_role_config(role: str, root: Path | None = None) -> dict:
    return dict(load_model_config(root)["roles"][ALIASES.get(role, role)])


def load_role_models(root: Path | None = None) -> dict[str, tuple[str, str]]:
    config = load_model_config(root)
    entries = dict(config["roles"])
    entries.update({alias: entries[task] for alias, task in ALIASES.items()})
    return {name: (entry["model"], entry["reasoning_effort"]) for name, entry in entries.items()}


def role_fingerprint(role: str, root: Path | None = None) -> str:
    """Only output-affecting routing; timeout and other tasks do not invalidate results."""
    value = load_model_config(root)
    entry = value["roles"][ALIASES.get(role, role)]
    semantic = {"role": ALIASES.get(role, role), "model": entry["model"],
                "reasoning_effort": entry["reasoning_effort"], "cache_epoch": value["cache_epoch"]}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
