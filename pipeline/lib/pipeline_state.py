"""
Cross-session state for interrupted pipelines.

Writes `pipeline/_state/interrupted_run.json` when a run starts, clears it
when the run ends cleanly. Claude sessions (via SKILL.md) should check this
file at startup and offer to resume.

Rationale: long pipelines (retime, rebuild) can be interrupted by Windows
updates / reboots / user Ctrl-C. The recovery path is often non-obvious
(which step, which topic, which mode). A small state file makes next-session
resumption a 1-check affair.
"""

import json
import os
import socket
from datetime import datetime
from pathlib import Path

from .atomic_io import atomic_write_json

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PIPELINE_ROOT / "_state"
INTERRUPTED_PATH = STATE_DIR / "interrupted_run.json"


def mark_running(mode, topic, command, extra=None):
    """Write a marker file indicating a run is in progress. Overwrites any
    previous marker (earlier runs are assumed completed; if they weren't,
    the marker from that run was not cleared and this new one replaces it
    — still informative because `started` timestamp captures currency)."""
    data = {
        "mode": mode,
        "topic": topic,
        "command": command,
        "started": datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "extra": extra or {},
    }
    atomic_write_json(INTERRUPTED_PATH, data)
    return INTERRUPTED_PATH


def mark_finished():
    """Clear the marker. Call at clean end of run (incl. failures where the
    operator acknowledges the outcome)."""
    try:
        if INTERRUPTED_PATH.exists():
            INTERRUPTED_PATH.unlink()
    except Exception:
        pass


def read_interrupted():
    """Return the marker dict if present, else None.
    Claude SKILL 에서 세션 시작 시 호출해 사용자에게 재개 안내."""
    if not INTERRUPTED_PATH.exists():
        return None
    try:
        return json.loads(INTERRUPTED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
