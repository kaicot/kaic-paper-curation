"""py312 단독 환경 가드.

운영자 지시(2026-06-18): paper-curation 은 **py312 단독**으로 사용한다. py314 등
다른 인터프리터로 진입하는 모든 경로를 차단한다. 각 실행 진입점(__main__)에서
``force_py312()`` 를 가장 먼저 호출하면, py312 가 아닌 인터프리터로 실행됐을 때
py312 인터프리터로 **자동 재실행**한다(py312 를 못 찾으면 명확히 실패).

라이브러리로 import 될 때(예: Paper Curio 브리지가 run_update_force 의 함수를
호출)는 영향이 없도록, 반드시 ``if __name__ == "__main__":`` 안에서만 호출한다.
"""

import os
import shutil
import sys
from pathlib import Path


def find_py312() -> str | None:
    """py312 인터프리터 경로를 찾는다. 우선순위:
    PAPER_CURATION_PY312 → 형제 conda env <base>/envs/py312 → which python3.12 →
    현재 인터프리터가 py312 면 그것. 없으면 None.
    """
    explicit = os.environ.get("PAPER_CURATION_PY312", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    here = Path(sys.executable).resolve()
    for anc in here.parents:
        if anc.name == "envs":
            cand = anc / "py312" / "bin" / "python"
            if cand.exists():
                return str(cand)
            break
    found = shutil.which("python3.12")
    if found:
        return found
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    return None


def force_py312() -> None:
    """현재 인터프리터가 py312 가 아니면 py312 로 재실행한다(py314 차단)."""
    if sys.version_info[:2] == (3, 12):
        return
    if os.environ.get("_PC_PY312_REEXEC") == "1":
        # 이미 한 번 재실행했는데도 py312 가 아니다 → 무한 루프 방지 + 명확히 실패.
        raise SystemExit(
            "paper-curation 은 py312 단독 환경을 사용합니다 (py314 금지). "
            f"현재 인터프리터: {sys.executable} (Python {sys.version.split()[0]})"
        )
    py312 = find_py312()
    if not py312:
        raise SystemExit(
            "py312 인터프리터를 찾을 수 없습니다. paper-curation 은 py312 단독 환경입니다 "
            "(py314 금지). conda env py312 를 만들거나 PAPER_CURATION_PY312 로 절대 경로를 지정하세요."
        )
    os.environ["_PC_PY312_REEXEC"] = "1"
    print(f"[env] py312 단독 강제: {sys.executable} → {py312} 로 재실행", file=sys.stderr)
    os.execv(py312, [py312, *sys.argv])
