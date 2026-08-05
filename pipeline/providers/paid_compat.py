"""Metadata-only quarantine for usage-billed provider compatibility names."""

from __future__ import annotations

from types import MappingProxyType as _MappingProxyType


PAID_PROVIDER_QUARANTINE = _MappingProxyType(
    {
        "anthropic": "disabled",
        "google.genai": "disabled",
        "openai": "disabled",
        "resend": "disabled",
    }
)
__all__ = ("PAID_PROVIDER_QUARANTINE",)


def __getattr__(name: str) -> object:
    raise AttributeError(f"paid provider compatibility is quarantined: {name}")
