"""
Windows 시작 시 자동 실행 등록/해제.

일반적인 '시작 프로그램 폴더'에 바로가기를 넣는 방식은, 이 앱이 관리자 권한
매니페스트(uac_admin=True)를 가지고 있어서 부팅마다 UAC 승인 창이 뜬다.

대신 작업 스케줄러(schtasks)에 '가장 높은 권한으로 실행(RL HIGHEST)' +
'로그온 시(SC ONLOGON)' 트리거로 등록하면, 이미 관리자 계정으로 로그온한
세션에서는 UAC 프롬프트 없이 조용히 관리자 권한으로 실행된다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "LGLocalManager_AutoStart"


def _exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    # 개발 중(파이썬 스크립트로 실행)에는 등록해도 의미가 없으니 안내만 한다.
    raise RuntimeError(
        "빌드된 exe에서만 시작 프로그램 등록이 가능합니다 (python -m app.main 로는 불가)."
    )


def is_registered() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def register() -> None:
    exe = _exe_path()
    subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN", TASK_NAME,
            "/TR", f'"{exe}"',
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/F",  # 이미 있으면 덮어쓰기
        ],
        check=True,
    )


def unregister() -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        check=False,  # 없으면 실패해도 무시
    )
