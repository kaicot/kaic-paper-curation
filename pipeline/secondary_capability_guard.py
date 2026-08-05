"""Import-safe denial boundary for unsupported secondary capabilities."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Final, Never, cast, override


ALLOWED_MODES: Final = frozenset({"codex", "off"})
CAPABILITY_STATUS: Final = "unavailable"


@dataclass(frozen=True, slots=True)
class SecondaryCapabilityUnavailable(RuntimeError):
    capability: str
    mode: str
    code: str = "secondary-capability-unavailable"

    @override
    def __str__(self) -> str:
        return f"{self.code}:{self.capability}:{self.mode}"


def denial_payload(capability: str, mode: str) -> dict[str, object]:
    if mode not in ALLOWED_MODES:
        raise SecondaryCapabilityUnavailable(
            capability,
            mode,
            "runtime-mode-denied",
        )
    return {
        "capability": capability,
        "mode": mode,
        "reason": "not-available-in-local-safe-profile",
        "status": CAPABILITY_STATUS,
    }


def deny(capability: str, mode: str = "codex") -> Never:
    raise SecondaryCapabilityUnavailable(capability, mode)


def cli(capability: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"{capability} is unavailable in the local-safe profile"
    )
    _ = parser.add_argument(
        "--llm-mode",
        choices=sorted(ALLOWED_MODES),
        default="codex",
    )
    _ = parser.add_argument("--capability-probe", action="store_true")
    namespace, _unknown = parser.parse_known_args(argv)
    mode = cast(str, namespace.llm_mode)
    print(
        json.dumps(
            denial_payload(capability, mode),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2
