"""Frozen, fail-closed runtime policy for local paper curation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias


RuntimeMode: TypeAlias = Literal["codex", "off"]
JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

DEFAULT_MODE: Final[RuntimeMode] = "codex"
PAID_CAPABILITIES: Final[tuple[str, ...]] = (
    "anthropic_api",
    "browser_byok",
    "cloudflare_deploy",
    "dense_retrieval",
    "google_gemini_api",
    "image_generation",
    "openai_api",
    "resend_email",
    "tts_audio",
    "web_deep_research",
)


@dataclass(frozen=True, slots=True)
class RuntimePolicyError(ValueError):
    """A policy boundary received a forbidden or malformed value."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Parsed runtime policy whose values cannot represent paid execution."""

    mode: RuntimeMode
    allow_paid_api: Literal[False] = False
    schema_version: Literal[2] = 2

    def config_value(self) -> JsonObject:
        """Return the canonical config-facing representation."""
        return {
            "allow_paid_api": self.allow_paid_api,
            "llm_mode": self.mode,
            "schema_version": self.schema_version,
        }

    def envelope(self) -> JsonObject:
        """Return the machine-consumed capability matrix."""
        capabilities: dict[str, JsonValue] = {
            "codex_generation": {
                "allowed": self.mode == "codex",
                "provider": "saved-chatgpt-auth-codex-cli",
            }
        }
        capabilities.update(
            {
                name: {"allowed": False, "disposition": "default-denied"}
                for name in PAID_CAPABILITIES
            }
        )
        return {
            "allow_paid_api": self.allow_paid_api,
            "canonical_child_argv": ["--llm-mode", self.mode],
            "capabilities": capabilities,
            "counters": zero_counters(),
            "mode": self.mode,
            "roles": {
                "long_form": {"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"},
                "short_form": {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
            },
            "schema": "runtime-policy-v2",
            "schema_version": 2,
            "status": "allowed",
        }


def zero_counters() -> JsonObject:
    """Return the fixed observable side-effect counters."""
    return {"credential_reads": 0, "egress": 0, "provider_imports": 0, "writes": 0}


def _runtime_mapping(config: JsonObject) -> JsonObject:
    value = config.get("runtime", {})
    if not isinstance(value, dict):
        raise RuntimePolicyError(code="invalid-runtime", detail="runtime must be a JSON object")
    return value


def resolve_runtime_policy(config: JsonObject, cli_mode: str | None = None, paid_acknowledged: bool = False) -> RuntimePolicy:
    """Parse config and CLI policy with CLI > config > frozen default precedence."""
    schema_version = config.get("schema_version", 2)
    if schema_version != 2:
        raise RuntimePolicyError(code="unsupported-schema", detail="schema_version must be 2")
    runtime = _runtime_mapping(config)
    forbidden_provider = runtime.get("api_provider", config.get("api_provider"))
    if forbidden_provider is not None:
        raise RuntimePolicyError(code="paid-provider-forbidden", detail="api_provider is unsupported")
    paid_value = runtime.get("allow_paid_api", config.get("allow_paid_api", False))
    if paid_value is not False:
        raise RuntimePolicyError(code="paid-api-forbidden", detail="allow_paid_api must be false")
    if paid_acknowledged:
        raise RuntimePolicyError(code="paid-ack-forbidden", detail="paid API acknowledgement is unsupported")
    configured_mode = runtime.get("llm_mode", DEFAULT_MODE)
    selected = cli_mode if cli_mode is not None else configured_mode
    if not isinstance(selected, str) or selected not in ("codex", "off"):
        raise RuntimePolicyError(code="unsupported-mode", detail="llm_mode must be codex or off")
    mode: RuntimeMode = selected
    return RuntimePolicy(mode=mode)


def denial_envelope(error: RuntimePolicyError) -> JsonObject:
    """Return a schema-valid denial without exposing config or environment values."""
    return {
        "allow_paid_api": False,
        "canonical_child_argv": [],
        "capabilities": {
            "codex_generation": {"allowed": False, "disposition": "policy-denied"},
            **{name: {"allowed": False, "disposition": "default-denied"} for name in PAID_CAPABILITIES},
        },
        "counters": zero_counters(),
        "denial": {"code": error.code, "detail": error.detail},
        "mode": None,
        "schema": "runtime-policy-v2",
        "schema_version": 2,
        "status": "denied",
    }
