"""Best-effort pipeline token-usage reporter.

Posts per-call token counts (from google-genai ``usage_metadata``) to the local
OpenTelemetry usage receiver's ``/pc/usage`` endpoint, which bins them into
5-minute buckets for the API-usage (종량제) dashboard graph.

Two capture paths, deduped so they never double-count the same call:
  1. Explicit ``record_gemini(resp, model)`` after a generate_content call.
  2. ``instrument_genai()`` monkey-patches google-genai so *every*
     ``generate_content`` / ``generate_images`` call auto-reports. This is what
     captures **PaperBanana**'s in-process Gemini calls — its planner /
     visualizer / critic / stylist agents and the nano-banana image model each
     build their own ``genai.Client`` and never touch this module, so without
     the patch their tokens are invisible to the dashboard.

Every response is tagged after recording, so the two paths report a given
response at most once. Fire-and-forget on a daemon thread with a short timeout;
every failure is swallowed so instrumentation can never affect the pipeline run.

Endpoint: ``PC_USAGE_ENDPOINT`` (default ``http://localhost:4318/pc/usage``).
On the macmini this must point at the actual receiver, e.g.
``http://100.114.66.16:4318/pc/usage`` — otherwise posts hit a dead local port
and are silently dropped.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

ENDPOINT = os.environ.get("PC_USAGE_ENDPOINT", "http://localhost:4318/pc/usage")

# Marker set on a google-genai response once its usage has been reported, so the
# monkey-patch wrapper and any explicit record_gemini() call on the same
# response object don't double-count it.
_SEEN_ATTR = "_pc_usage_recorded"


def _post(payload: dict) -> None:
    def run():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                ENDPOINT, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        pass


def record_gemini(resp, model: str | None = None) -> None:
    """Extract token counts from a google-genai response and report them once."""
    try:
        if resp is None:
            return
        # Dedup: never report the same response twice (explicit call + patch).
        try:
            if getattr(resp, _SEEN_ATTR, False):
                return
        except Exception:
            pass

        um = getattr(resp, "usage_metadata", None)
        if um is None:
            return

        def g(*names):
            for n in names:
                v = getattr(um, n, None)
                if isinstance(v, (int, float)) and v:
                    return int(v)
            return 0

        by_type = {
            "input": g("prompt_token_count"),
            "output": g("candidates_token_count"),
            "cached": g("cached_content_token_count"),
            "reasoning": g("thoughts_token_count"),
        }
        by_type = {k: v for k, v in by_type.items() if v}

        # Tag as seen regardless of whether there were countable tokens, so a
        # token-less response isn't re-examined by the other code path.
        try:
            setattr(resp, _SEEN_ATTR, True)
        except Exception:
            pass

        if not by_type:
            return
        _post({"provider": "gemini", "by_type": by_type, "model": model})
    except Exception:
        pass


_instrumented = False
_instrument_lock = threading.Lock()


def instrument_genai() -> None:
    """Monkey-patch google-genai so every generate_content / generate_images
    call auto-reports Gemini token usage via record_gemini().

    Idempotent and failure-swallowing. Patches the ``Models`` (sync) and
    ``AsyncModels`` (async) classes, so every ``genai.Client`` in the process —
    including the ones PaperBanana's agents create — is covered.
    """
    global _instrumented
    try:
        with _instrument_lock:
            if _instrumented:
                return
            import asyncio

            try:
                from google.genai import models as _m
            except Exception:
                return

            def _model_of(kwargs):
                m = kwargs.get("model")
                return m if isinstance(m, str) else None

            def _wrap(cls, name, is_async):
                orig = getattr(cls, name, None)
                if orig is None or getattr(orig, "_pc_usage_wrapped", False):
                    return
                if is_async or asyncio.iscoroutinefunction(orig):
                    async def awrapped(self, *args, **kwargs):
                        resp = await orig(self, *args, **kwargs)
                        try:
                            record_gemini(resp, _model_of(kwargs))
                        except Exception:
                            pass
                        return resp

                    awrapped._pc_usage_wrapped = True
                    setattr(cls, name, awrapped)
                else:
                    def swrapped(self, *args, **kwargs):
                        resp = orig(self, *args, **kwargs)
                        try:
                            record_gemini(resp, _model_of(kwargs))
                        except Exception:
                            pass
                        return resp

                    swrapped._pc_usage_wrapped = True
                    setattr(cls, name, swrapped)

            for cls_name, is_async in (("Models", False), ("AsyncModels", True)):
                cls = getattr(_m, cls_name, None)
                if cls is None:
                    continue
                for meth in ("generate_content", "generate_images"):
                    _wrap(cls, meth, is_async)

            _instrumented = True
    except Exception:
        pass


# Auto-instrument on import so any module that does ``import usage_log`` gets
# full coverage. config_loader also calls this, which covers processes that only
# reach google-genai transitively (e.g. PaperBanana via generate_timelines).
instrument_genai()
