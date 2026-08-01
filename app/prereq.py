"""
실행에 필요한 드라이버/권한을 점검한다.

- Npcap: scapy의 raw packet 캡처/전송에 필요. 드라이버라서 최초 1회는
  사용자가 직접 설치해야 한다 (자동 무인 설치는 하지 않는다 — 시스템에
  드라이버를 몰래 깔지 않기 위함).
- WinDivert: .dll/.sys 는 앱 폴더에 그대로 동봉하면 되므로 별도 설치 불필요.
- openssl: rethink-cloud가 인증서 발급에 커맨드로 직접 사용한다. 앱 배포 zip에
  Git for Windows의 usr/(bin+ssl)을 runtime/openssl/ 로 번들해뒀다 — 1순위는
  그 번들, 그다음은 시스템에 이미 있는 것(PATH, 또는 별도 설치된 Git).
- 관리자 권한: ARP 스푸핑/패킷 재작성 모두 필요.
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import winreg
from pathlib import Path

NPCAP_DOWNLOAD_URL = "https://npcap.com/#download"

# bin/ssl을 형제 폴더로 유지해야 openssl.exe가 자기 설정 파일(openssl.cnf)을
# 상대 경로로 찾을 수 있다. find_openssl_dir()이 반환하는 건 항상 "bin" 폴더고,
# 설정 파일은 그 옆의 "ssl" 폴더에 있다고 가정한다 (openssl_cnf_path() 참고).
_OPENSSL_CANDIDATE_DIRS = (
    Path(r"C:\Program Files\Git\usr\bin"),
    Path(r"C:\Program Files (x86)\Git\usr\bin"),
)

# Npcap 드라이버 파일이 있을 법한 위치 (레지스트리로 못 찾을 때의 최종 확인용).
_NPCAP_FILE_CANDIDATES = (
    Path(r"C:\Windows\System32\Npcap\wpcap.dll"),
    Path(r"C:\Windows\System32\Npcap\Packet.dll"),
    Path(r"C:\Windows\System32\drivers\npcap.sys"),
)


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
    """
    Npcap 설치 여부를 확인한다.

    레지스트리 키 하나만 보면 오탐이 난다 — Npcap 설치 프로그램이 32비트로
    동작하면 Windows가 HKLM\\SOFTWARE\\Npcap 대신 HKLM\\SOFTWARE\\WOW6432Node\\Npcap
    으로 자동 리다이렉트해서 쓰는데, 이 둘은 서로 자동으로 연결되지 않는다.
    그래서 두 레지스트리 뷰를 모두 보고, 그것도 실패하면 실제 드라이버 파일
    존재 여부로 최종 확인한다.
    """
    registry_paths = (r"SOFTWARE\Npcap", r"SOFTWARE\WOW6432Node\Npcap")
    access_flags = (
        winreg.KEY_READ,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
    )
    for path in registry_paths:
        for flags in access_flags:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, flags):
                    return True
            except OSError:
                continue

    return any(candidate.exists() for candidate in _NPCAP_FILE_CANDIDATES)


def windivert_files_present(app_dir: Path) -> bool:
    return (app_dir / "WinDivert.dll").exists() and (
        app_dir / "WinDivert64.sys"
    ).exists()


def find_openssl_dir(app_dir: Path | None = None) -> Path | None:
    """openssl.exe가 있는 "bin" 폴더를 찾는다. 번들 → PATH → 시스템 Git 순으로 본다."""
    if app_dir is not None:
        bundled = app_dir / "runtime" / "openssl" / "bin" / "openssl.exe"
        if bundled.exists():
            return bundled.parent

    found_on_path = shutil.which("openssl")
    if found_on_path:
        return Path(found_on_path).parent

    for candidate_dir in _OPENSSL_CANDIDATE_DIRS:
        if (candidate_dir / "openssl.exe").exists():
            return candidate_dir
    return None


def openssl_cnf_path(openssl_bin_dir: Path) -> Path | None:
    """openssl.exe와 형제 관계인 ssl/openssl.cnf 경로를 찾는다. 없으면 None."""
    candidate = openssl_bin_dir.parent / "ssl" / "openssl.cnf"
    return candidate if candidate.exists() else None


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
    if find_openssl_dir(app_dir) is None:
        problems.append(
            "openssl.exe를 찾지 못했습니다. rethink-cloud가 인증서를 발급하지 못해 "
            "시작에 실패합니다. release 패키지가 손상되었을 수 있습니다 "
            "(runtime/openssl/bin/ 폴더 확인 필요) — 또는 Git for "
            "Windows(https://git-scm.com/download/win)를 설치하면 자동으로 인식합니다."
        )
    return problems
