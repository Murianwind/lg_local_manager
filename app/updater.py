"""
GitHub Releases 기반 업데이트 확인 + 자동 설치.

두 가지 채널을 구분한다:
  - stable: GitHub Release 발행(release: published) 트리거로 만들어진, 정식 태그
            버전의 릴리즈만 인식한다 (prerelease == False).
  - beta:   workflow_dispatch로 수동 빌드된 것(타임스탬프 태그, prerelease == True)
            까지 포함해 가장 최근 릴리즈를 인식한다.

실행 중인 exe는 자기 자신을 덮어쓸 수 없으므로, 새 버전을 내려받아 임시
폴더에 풀어놓은 뒤, 별도의 PowerShell 스크립트를 띄워서:
  1) 지금 프로세스(PID)가 완전히 끝날 때까지 기다리고
  2) config/data 안의 사용자 파일은 그대로 두고 나머지만 교체한 뒤
  3) LGLocalManager.exe 를 다시 실행하고
  4) 다운로드/압축 해제에 썼던 임시 폴더를 스스로 지운다.
"""

from __future__ import annotations

import logging
import os
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

# 업데이트 시 사용자 데이터를 보존하기 위해 건드리지 않을 상대 경로(폴더 단위).
PRESERVE_PATHS = ("config", "data")

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

    asset = next(
        (a for a in data.get("assets", []) if a["name"].lower().endswith(".zip")),
        None,
    )
    if asset is None:
        logger.warning("릴리즈 %s 에 zip 자산이 없습니다.", tag)
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
$sourceDir = "{source_dir}"
$targetDir = "{target_dir}"
$tmpDir = "{tmp_dir}"
$exeName = "LGLocalManager.exe"
$preserve = @({preserve_list})

# 1) 기존 프로세스가 완전히 끝날 때까지 대기 (최대 30초)
$count = 0
while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
    Start-Sleep -Milliseconds 500
    $count++
}}

# 2) 사용자 데이터(config/data)는 제외하고 나머지 파일을 교체
Get-ChildItem -Path $sourceDir -Force | ForEach-Object {{
    if ($preserve -contains $_.Name) {{
        return
    }}
    $destPath = Join-Path $targetDir $_.Name
    if (Test-Path $destPath) {{
        Remove-Item -Recurse -Force $destPath
    }}
    Copy-Item -Recurse -Force $_.FullName $destPath
}}

# 3) 재실행
Start-Process -FilePath (Join-Path $targetDir $exeName)

# 4) 다운로드/압축 해제에 쓴 임시 폴더 정리 (zip, 압축 해제본 포함)
Start-Sleep -Seconds 2
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
'''


def apply_update(info: UpdateInfo, app_dir: Path, on_before_exit=None) -> None:
    """새 버전을 내려받아 적용 스크립트를 띄우고, 앱을 종료시킨다."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="lglocalmanager-update-"))
    zip_path = tmp_dir / "update.zip"
    extract_dir = tmp_dir / "extracted"

    logger.info("업데이트 다운로드 시작: %s", info.download_url)
    _download(info.download_url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # zip 안에 폴더 한 겹이 더 있을 수도, 없을 수도 있으니 exe가 있는 위치를 찾는다.
    source_dir = extract_dir
    if not (source_dir / "LGLocalManager.exe").exists():
        candidates = [
            d for d in extract_dir.iterdir() if d.is_dir() and (d / "LGLocalManager.exe").exists()
        ]
        if candidates:
            source_dir = candidates[0]
        else:
            raise RuntimeError("압축 해제한 파일에서 LGLocalManager.exe를 찾지 못했습니다.")

    preserve_list = ", ".join(f'"{p}"' for p in PRESERVE_PATHS)
    script_content = _SWAP_SCRIPT_TEMPLATE.format(
        pid=os.getpid(),
        source_dir=str(source_dir),
        target_dir=str(app_dir),
        tmp_dir=str(tmp_dir),
        preserve_list=preserve_list,
    )
    # 스크립트 자신은 tmp_dir '밖'에 둔다 (안에 두면 자기 자신을 지우는 동안 잠길 수 있음).
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
