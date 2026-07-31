"""
GitHub Releases 기반 업데이트 확인 + 자동 설치.

릴리즈마다 zip이 두 개 올라온다 (.github/workflows/build.yml 참고):
  - LGLocalManager-Full-*.zip   : 처음 설치용, runtime/(Node, rethink)와 WinDivert
                                   파일까지 포함한 전체 패키지 (용량 큼)
  - LGLocalManager-Update-*.zip : LGLocalManager.exe 하나만 (앱 코드가 바뀌는
                                   업데이트는 이 exe 하나만 바뀌므로, runtime은
                                   그대로 두고 이것만 받으면 됨 — 훨씬 작고 빠름)

이 모듈은 항상 "Update" zip을 찾아서 그것만 받는다. exe 하나만 교체하면 되므로
config/data 보존 로직도 필요 없다 — 애초에 그 폴더들을 건드리지 않는다.

두 가지 채널을 구분한다:
  - stable: GitHub Release 발행(release: published) 트리거로 만들어진, 정식 태그
            버전의 릴리즈만 인식한다 (prerelease == False).
  - beta:   workflow_dispatch로 수동 빌드된 것(타임스탬프 태그, prerelease == True)
            까지 포함해 가장 최근 릴리즈를 인식한다.

실행 중인 exe는 자기 자신을 덮어쓸 수 없으므로, 새 exe를 내려받아 임시 폴더에
풀어놓은 뒤, 별도의 짧은 PowerShell 스크립트를 띄워서:
  1) 지금 프로세스(PID)가 완전히 끝날 때까지 기다리고
  2) LGLocalManager.exe 파일 하나만 교체한 뒤
  3) 다시 실행하고
  4) 다운로드에 썼던 임시 폴더를 스스로 지운다.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests

from .version import APP_VERSION

logger = logging.getLogger("updater")

# "owner/repo" 형태. 실제 저장소 이름에 맞게 바꾸세요.
GITHUB_REPO = "Murianwind/lg_local_manager"
API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# release 자산 중 "업데이트용(exe만)" zip을 골라낼 때 쓰는 패턴.
UPDATE_ASSET_PATTERN = re.compile(r"update", re.IGNORECASE)

UpdateChannel = Literal["stable", "beta"]


@dataclass
class UpdateInfo:
    tag_name: str
    version: str
    download_url: str
    notes: str
    prerelease: bool


def _parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return _parse_version(remote) > _parse_version(local)


def _fetch_releases(timeout: float) -> list[dict]:
    resp = requests.get(API_RELEASES, params={"per_page": 20}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _pick_release(releases: list[dict], channel: UpdateChannel) -> dict | None:
    if not releases:
        return None
    # GitHub API는 기본적으로 최신 생성순으로 내려준다.
    candidates = [r for r in releases if not r.get("draft")]
    if channel == "stable":
        candidates = [r for r in candidates if not r.get("prerelease")]
    if not candidates:
        return None
    return candidates[0]


def _pick_update_asset(assets: list[dict]) -> dict | None:
    """'Update' 이름이 들어간 zip 자산을 우선 찾는다."""
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    update_assets = [a for a in zips if UPDATE_ASSET_PATTERN.search(a["name"])]
    if update_assets:
        return update_assets[0]
    logger.warning(
        "Update 전용 zip을 찾지 못했습니다. 릴리즈 자산 이름에 'update'가 포함되어야 합니다."
    )
    return None


def check_for_update(
    channel: UpdateChannel = "stable", timeout: float = 10.0
) -> UpdateInfo | None:
    """선택한 채널 기준으로 최신 릴리즈를 조회한다. 새 버전이 없으면 None."""
    try:
        releases = _fetch_releases(timeout)
    except Exception as e:  # noqa: BLE001
        logger.warning("업데이트 확인 실패: %s", e)
        return None

    data = _pick_release(releases, channel)
    if data is None:
        return None

    tag = data.get("tag_name", "")
    if not tag or not is_newer(tag):
        return None

    asset = _pick_update_asset(data.get("assets", []))
    if asset is None:
        return None

    return UpdateInfo(
        tag_name=tag,
        version=tag.lstrip("vV"),
        download_url=asset["browser_download_url"],
        notes=data.get("body", ""),
        prerelease=bool(data.get("prerelease")),
    )


def _download(url: str, dest: Path, timeout: float = 60.0) -> None:
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


_SWAP_SCRIPT_TEMPLATE = r'''
$ErrorActionPreference = "Stop"
$pid_to_wait = {pid}
$newExe = "{new_exe}"
$targetExe = "{target_exe}"
$tmpDir = "{tmp_dir}"

# 1) 기존 프로세스가 완전히 끝날 때까지 대기 (최대 30초)
$count = 0
while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
    Start-Sleep -Milliseconds 500
    $count++
}}

# 2) exe 하나만 교체 (다른 파일/폴더는 건드리지 않음)
Copy-Item -Force $newExe $targetExe

# 3) 재실행
Start-Process -FilePath $targetExe

# 4) 다운로드에 쓴 임시 폴더 정리
Start-Sleep -Seconds 2
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
'''


def apply_update(info: UpdateInfo, app_dir: Path, on_before_exit=None) -> None:
    """새 exe를 내려받아 적용 스크립트를 띄우고, 앱을 종료시킨다."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="lglocalmanager-update-"))
    zip_path = tmp_dir / "update.zip"
    extract_dir = tmp_dir / "extracted"

    logger.info("업데이트 다운로드 시작: %s", info.download_url)
    _download(info.download_url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    new_exe = extract_dir / "LGLocalManager.exe"
    if not new_exe.exists():
        candidates = list(extract_dir.rglob("LGLocalManager.exe"))
        if not candidates:
            raise RuntimeError("압축 해제한 파일에서 LGLocalManager.exe를 찾지 못했습니다.")
        new_exe = candidates[0]

    script_content = _SWAP_SCRIPT_TEMPLATE.format(
        pid=os.getpid(),
        new_exe=str(new_exe),
        target_exe=str(app_dir / "LGLocalManager.exe"),
        tmp_dir=str(tmp_dir),
    )
    # 스크립트 자신은 tmp_dir '밖'에 둔다 (tmp_dir을 지울 때 자기 자신이 잠기지 않도록).
    script_path = Path(tempfile.gettempdir()) / f"lglocalmanager_apply_{os.getpid()}.ps1"
    script_path.write_text(script_content, encoding="utf-8")

    logger.info("업데이트 적용 스크립트 실행 후 종료합니다: %s -> %s", APP_VERSION, info.version)
    subprocess.Popen(
        [
            "powershell",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script_path),
        ],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    if on_before_exit:
        on_before_exit()

    # 정상 종료 절차를 앱(main.py)이 이어서 수행하도록, 여기선 트리거만 한다.
