"""
실행에 필요한 드라이버/권한을 점검한다.

- Npcap: scapy의 raw packet 캡처/전송에 필요. 드라이버라서 최초 1회는
  사용자가 직접 설치해야 한다 (자동 무인 설치는 하지 않는다 — 시스템에
  드라이버를 몰래 깔지 않기 위함).
- WinDivert: .dll/.sys 는 앱 폴더에 그대로 동봉하면 되므로 별도 설치 불필요.
- 관리자 권한: ARP 스푸핑/패킷 재작성 모두 필요.
"""

from __future__ import annotations

import ctypes
import os
import sys
import winreg
from pathlib import Path


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def relaunch_as_admin() -> None:
    """관리자 권한으로 재실행한다 (UAC 프롬프트가 뜬다)."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    sys.exit(0)


def npcap_installed() -> bool:
    """레지스트리로 Npcap 설치 여부를 간단히 확인한다."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Npcap"
        ):
            return True
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001
        return False


def windivert_files_present(app_dir: Path) -> bool:
    return (app_dir / "WinDivert.dll").exists() and (
        app_dir / "WinDivert64.sys"
    ).exists()


NPCAP_DOWNLOAD_URL = "https://npcap.com/#download"


def check_all(app_dir: Path) -> list[str]:
    """문제 목록을 반환한다. 비어 있으면 실행 준비 완료."""
    problems: list[str] = []
    if not is_admin():
        problems.append("관리자 권한으로 실행되지 않았습니다.")
    if not npcap_installed():
        problems.append(
            f"Npcap이 설치되어 있지 않습니다. {NPCAP_DOWNLOAD_URL} 에서 설치 후 "
            "다시 실행해주세요. (설치 시 'WinPcap API-compatible Mode' 체크 필요)"
        )
    if not windivert_files_present(app_dir):
        problems.append(
            "WinDivert.dll / WinDivert64.sys 파일을 찾을 수 없습니다. "
            "release 패키지가 손상되었을 수 있습니다."
        )
    return problems
