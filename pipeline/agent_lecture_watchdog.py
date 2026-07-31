#!/usr/bin/env python3
"""Watchdog for the autonomous lecture digest launchd job.

Runs quickly from launchd every few minutes.  It prevents schedule drift by
restarting digest jobs that freeze during startup and by starting an overdue
pending lecture if launchd missed the calendar fire.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(os.environ.get("PC_REPO_DIR", str(Path(__file__).resolve().parent.parent))).expanduser()
AGENT_DIR = Path(os.environ.get("PC_AGENT_DIR", str(Path.home() / "pc_agent" / "dashun_wang"))).expanduser()
LEDGER = AGENT_DIR / "curriculum.json"
OUTDIR = AGENT_DIR / "lectures"
RUNNER = Path.home() / "pc_agent" / "run_digest.sh"
LOCK = AGENT_DIR / "watchdog.lock"
STATE = AGENT_DIR / "watchdog_state.json"
LOG = AGENT_DIR / "watchdog.log"

STARTUP_FREEZE_AFTER = int(os.environ.get("PC_DIGEST_STARTUP_FREEZE_AFTER", "480"))  # 8 min
STARTUP_CPU_MAX = float(os.environ.get("PC_DIGEST_STARTUP_CPU_MAX", "0.30"))
STALL_AFTER = int(os.environ.get("PC_DIGEST_STALL_AFTER", "2700"))                  # 45 min
MISSED_START_GRACE = int(os.environ.get("PC_DIGEST_MISSED_START_GRACE", "420"))     # 7 min


@dataclass
class Proc:
    pid: int
    ppid: int
    elapsed: int
    cpu: float
    stat: str
    cmd: str


def log(msg: str) -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {msg}\n")
    print(msg, flush=True)


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def parse_cpu_time(s: str) -> float:
    # macOS ps TIME can be mm:ss, hh:mm:ss, or dd-hh:mm:ss.
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d or 0)
    raw = s.split(":")
    try:
        if len(raw) == 2:
            h, m, sec = 0, int(raw[0]), float(raw[1])
        elif len(raw) == 3:
            h, m, sec = int(raw[0]), int(raw[1]), float(raw[2])
        else:
            return 0.0
    except ValueError:
        return 0.0
    return float(days * 86400 + h * 3600 + m * 60 + sec)


def digest_procs() -> list[Proc]:
    try:
        cp = run(["/bin/ps", "-ww", "-axo", "pid=,ppid=,etime=,time=,stat=,command="], timeout=15)
    except Exception as e:
        log(f"ps failed: {e}")
        return []
    out: list[Proc] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        cmd = parts[5]
        exe = cmd.split(None, 1)[0] if cmd else ""
        if "python" not in Path(exe).name or "pipeline/agent_lecture_digest.py" not in cmd:
            continue
        if "agent_lecture_watchdog.py" in cmd:
            continue
        try:
            out.append(Proc(int(parts[0]), int(parts[1]), int(parse_cpu_time(parts[2])), parse_cpu_time(parts[3]), parts[4], parts[5]))
        except Exception:
            continue
    return sorted(out, key=lambda p: p.elapsed, reverse=True)


def tcp_count(pid: int) -> int:
    lsof = shutil.which("lsof")
    if not lsof:
        return 0
    try:
        cp = run([lsof, "-nP", "-p", str(pid)], timeout=20)
    except Exception:
        return 0
    return sum(1 for line in cp.stdout.splitlines() if " TCP " in line and "LISTEN" not in line)


def load_ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def due_lecture(led: dict, grace: int = 0) -> dict | None:
    now = datetime.now() - timedelta(seconds=grace)
    due = []
    for L in led.get("lectures", []):
        if L.get("status") == "done":
            continue
        try:
            sched = datetime.strptime(L["scheduled_at"], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if sched <= now:
            due.append(L)
    return min(due, key=lambda x: x["scheduled_at"]) if due else None


def proc_lecture(proc: Proc, led: dict) -> int | None:
    m = re.search(r"--lecture\s+(\d+)", proc.cmd)
    if m:
        return int(m.group(1))
    L = due_lecture(led, grace=0)
    return int(L["lecture"]) if L else None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_many(procs: list[Proc], reason: str) -> None:
    if not procs:
        return
    log(f"killing digest pids {[p.pid for p in procs]} ({reason})")
    for p in procs:
        try:
            os.kill(p.pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(8)
    for p in procs:
        if alive(p.pid):
            try:
                os.kill(p.pid, signal.SIGKILL)
            except OSError:
                pass


def start_lecture(n: int, reason: str) -> int:
    if not RUNNER.exists():
        raise RuntimeError(f"runner missing: {RUNNER}")
    log_path = AGENT_DIR / f"watchdog_l{n:02d}.log"
    log(f"starting lecture {n} ({reason}); log={log_path}")
    f = log_path.open("ab")
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    p = subprocess.Popen([str(RUNNER), "--lecture", str(n)], cwd=str(REPO), stdout=f,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    return p.pid


def artifact_marker(n: int | None) -> dict:
    files = []
    if n:
        files.extend(sorted(OUTDIR.glob(f"lecture_{n:02d}.*")))
        files.extend(sorted(OUTDIR.glob(f"lecture_{n:02d}_*")))
        files.append(AGENT_DIR / f"watchdog_l{n:02d}.log")
    files.extend([AGENT_DIR / "launchd.out", AGENT_DIR / "launchd.err"])
    out = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_size, int(st.st_mtime)))
    return {"files": sorted(set(out))}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        if not LEDGER.exists():
            log(f"ledger missing: {LEDGER}")
            return 1
        led = load_ledger()
        procs = digest_procs()

        if len(procs) > 1:
            L = due_lecture(led, grace=0)
            n = int(L["lecture"]) if L else proc_lecture(procs[0], led)
            kill_many(procs, "duplicate digest processes")
            if n:
                start_lecture(n, "duplicate cleanup")
            return 0

        if not procs:
            L = due_lecture(led, grace=MISSED_START_GRACE)
            if L:
                start_lecture(int(L["lecture"]), f"missed/overdue by >= {MISSED_START_GRACE}s")
            save_state({})
            return 0

        p = procs[0]
        n = proc_lecture(p, led)
        tcp = tcp_count(p.pid)

        if p.elapsed >= STARTUP_FREEZE_AFTER and p.cpu <= STARTUP_CPU_MAX and tcp == 0:
            kill_many([p], f"startup freeze: elapsed={p.elapsed}s cpu={p.cpu:.2f}s tcp=0")
            if n:
                start_lecture(n, "startup freeze restart")
            save_state({})
            return 0

        now = time.time()
        marker = artifact_marker(n)
        marker["cpu"] = round(p.cpu, 1)
        marker["tcp"] = tcp
        state = load_state()
        same_pid = state.get("pid") == p.pid
        last_marker = state.get("marker") if same_pid else None
        last_progress = float(state.get("last_progress", now)) if same_pid else now

        progressed = (last_marker != marker) or tcp > 0
        if progressed:
            last_progress = now

        if same_pid and now - last_progress >= STALL_AFTER:
            kill_many([p], f"no progress for {int(now - last_progress)}s (lecture={n}, cpu={p.cpu:.1f}, tcp={tcp})")
            if n:
                start_lecture(n, "stall restart")
            save_state({})
            return 0

        save_state({"pid": p.pid, "lecture": n, "first_seen": state.get("first_seen", now) if same_pid else now,
                    "last_progress": last_progress, "marker": marker})
        log(f"ok pid={p.pid} lecture={n} elapsed={p.elapsed}s cpu={p.cpu:.1f}s tcp={tcp}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
