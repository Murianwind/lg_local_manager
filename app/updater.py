"""
GitHub Releases 기반 업데이트 확인 + 자동 설치.

릴리즈마다 세 가지 자산이 올라온다 (.github/workflows/build.yml 참고):
  - LGLocalManager-Full-*.zip     : 처음 설치용, runtime/(Node, rethink)와 WinDivert
                                     파일까지 포함한 전체 패키지 (용량 큼)
  - LGLocalManager-Update-*.zip   : LGLocalManager.exe 하나만 (앱 코드만 바뀌었을 때)
  - runtime-manifest.json          : Node 버전, WinDivert 버전, rethink 커밋 해시를
                                     담은 작은 지문 파일

동작 원리 (Full/Update 자동 판단):
  1) 새 릴리즈의 runtime-manifest.json만 먼저 받아온다 (zip을 통째로 받지 않고,
     이 작은 파일 하나만 조회하므로 빠르다).
  2) 앱 폴더에 있는 로컬 runtime-manifest.json과 비교한다.
     - 완전히 같다 -> runtime(Node/rethink/WinDivert)은 안 바뀐 것 -> Update zip(exe만)
     - 다르거나 로컬 파일이 없다 -> runtime이 바뀐 것 -> Full zip(전체 재설치)
  3) Full 설치일 때는 config/data(사용자 설정·기기 목록·로그)는 보존하고 나머지
     전부를 교체한다. Update 설치일 때는 exe 파일 하나만 교체한다.

두 가지 배포 채널도 구분한다:
  - stable: GitHub Release 발행(release: published)으로 만들어진 정식 태그만 인식
            (prerelease == False)
  - beta:   workflow_dispatch 수동 빌드(타임스탬프 태그, prerelease == True)까지 포함
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

MANIFEST_ASSET_NAME = "runtime-manifest.json"
UPDATE_ASSET_PATTERN = re.compile(r"update", re.IGNORECASE)
FULL_ASSET_PATTERN = re.compile(r"full", re.IGNORECASE)

# Full 설치 시 보존할(교체하지 않을) 최상위 경로.
PRESERVE_PATHS = ("config", "data")

UpdateChannel = Literal["stable", "beta"]


@dataclass
class UpdateInfo:
    tag_name: str
    version: str
    notes: str
    prerelease: bool
    update_zip_url: str | None
    full_zip_url: str | None
    requires_full: bool


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
    candidates = [r for r in releases if not r.get("draft")]
    if channel == "stable":
        candidates = [r for r in candidates if not r.get("prerelease")]
    if not candidates:
        return None
    return candidates[0]  # GitHub API는 최신 생성순으로 내려준다.


def _find_asset(assets: list[dict], pattern: re.Pattern, ext: str = ".zip") -> dict | None:
    matches = [
        a for a in assets if a["name"].lower().endswith(ext) and pattern.search(a["name"])
    ]
    return matches[0] if matches else None


def _fetch_json_asset(asset: dict, timeout: float) -> dict | None:
    try:
        resp = requests.get(asset["browser_download_url"], timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("매니페스트(%s) 조회 실패: %s", asset.get("name"), e)
        return None


def _local_manifest_path(app_dir: Path) -> Path:
    return app_dir / MANIFEST_ASSET_NAME


def _read_local_manifest(app_dir: Path) -> dict | None:
    path = _local_manifest_path(app_dir)
    if not path.exists():
        return None
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("로컬 runtime-manifest.json 읽기 실패: %s", e)
        return None


def check_for_update(
    app_dir: Path, channel: UpdateChannel = "stable", timeout: float = 10.0
) -> UpdateInfo | None:
    """
    선택한 채널 기준으로 최신 릴리즈를 조회한다.
    새 버전이 없으면 None. 있으면 Full/Update 여부까지 판단해서 반환한다.
    """
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

    assets = data.get("assets", [])
    update_asset = _find_asset(assets, UPDATE_ASSET_PATTERN)
    full_asset = _find_asset(assets, FULL_ASSET_PATTERN)
    manifest_asset = next(
        (a for a in assets if a["name"] == MANIFEST_ASSET_NAME), None
    )

    requires_full = True  # 매니페스트를 못 구하면 안전하게 Full로 판단
    if manifest_asset is not None:
        remote_manifest = _fetch_json_asset(manifest_asset, timeout)
        local_manifest = _read_local_manifest(app_dir)
        if remote_manifest is not None and local_manifest is not None:
            requires_full = remote_manifest != local_manifest
        elif remote_manifest is not None and local_manifest is None:
            logger.info("로컬 매니페스트가 없어 Full 설치로 판단합니다 (최초 설치 이후 처음 업데이트?).")
            requires_full = True
        else:
            logger.warning("원격 매니페스트를 못 구해 안전하게 Full 설치로 판단합니다.")
            requires_full = True
    else:
        logger.warning("릴리즈에 runtime-manifest.json이 없어 안전하게 Full 설치로 판단합니다.")

    if requires_full and full_asset is None:
        logger.error("Full 설치가 필요하지만 Full zip 자산을 찾지 못했습니다.")
        return None
    if not requires_full and update_asset is None:
        # Update 전용 zip이 없으면 Full로라도 대체
        if full_asset is not None:
            logger.warning("Update 전용 zip이 없어 Full zip으로 대체합니다.")
            requires_full = True
        else:
            logger.error("Update/Full zip 모두 찾지 못했습니다.")
            return None

    return UpdateInfo(
        tag_name=tag,
        version=tag.lstrip("vV"),
        notes=data.get("body", ""),
        prerelease=bool(data.get("prerelease")),
        update_zip_url=update_asset["browser_download_url"] if update_asset else None,
        full_zip_url=full_asset["browser_download_url"] if full_asset else None,
        requires_full=requires_full,
    )


def _download(url: str, dest: Path, timeout: float = 120.0) -> None:
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


_LIGHT_SWAP_SCRIPT = r'''
$ErrorActionPreference = "Stop"
$pid_to_wait = {pid}
$newExe = "{new_exe}"
$targetExe = "{target_exe}"
$tmpDir = "{tmp_dir}"

$count = 0
while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
    Start-Sleep -Milliseconds 500
    $count++
}}

Copy-Item -Force $newExe $targetExe
Start-Process -FilePath $targetExe

Start-Sleep -Seconds 2
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
'''

_FULL_SWAP_SCRIPT = r'''
$ErrorActionPreference = "Stop"
$pid_to_wait = {pid}
$sourceDir = "{source_dir}"
$targetDir = "{target_dir}"
$tmpDir = "{tmp_dir}"
$exeName = "LGLocalManager.exe"
$preserve = @({preserve_list})

$count = 0
while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
    Start-Sleep -Milliseconds 500
    $count++
}}

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

Start-Process -FilePath (Join-Path $targetDir $exeName)

Start-Sleep -Seconds 2
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
'''


def _launch_script(script_content: str) -> None:
    script_path = Path(tempfile.gettempdir()) / f"lglocalmanager_apply_{os.getpid()}.ps1"
    script_path.write_text(script_content, encoding="utf-8")
    subprocess.Popen(
        [
            "powershell",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script_path),
        ],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def _apply_light_update(info: UpdateInfo, app_dir: Path) -> None:
    assert info.update_zip_url is not None
    tmp_dir = Path(tempfile.mkdtemp(prefix="lglocalmanager-update-"))
    zip_path = tmp_dir / "update.zip"
    extract_dir = tmp_dir / "extracted"

    logger.info("Update(exe만) 다운로드: %s", info.update_zip_url)
    _download(info.update_zip_url, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    new_exe = extract_dir / "LGLocalManager.exe"
    if not new_exe.exists():
        candidates = list(extract_dir.rglob("LGLocalManager.exe"))
        if not candidates:
            raise RuntimeError("Update zip에서 LGLocalManager.exe를 찾지 못했습니다.")
        new_exe = candidates[0]

    script = _LIGHT_SWAP_SCRIPT.format(
        pid=os.getpid(),
        new_exe=str(new_exe),
        target_exe=str(app_dir / "LGLocalManager.exe"),
        tmp_dir=str(tmp_dir),
    )
    _launch_script(script)


def _apply_full_update(info: UpdateInfo, app_dir: Path) -> None:
    assert info.full_zip_url is not None
    tmp_dir = Path(tempfile.mkdtemp(prefix="lglocalmanager-update-"))
    zip_path = tmp_dir / "update.zip"
    extract_dir = tmp_dir / "extracted"

    logger.info("Full(전체) 다운로드: %s", info.full_zip_url)
    _download(info.full_zip_url, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    source_dir = extract_dir
    if not (source_dir / "LGLocalManager.exe").exists():
        candidates = [
            d for d in extract_dir.iterdir() if d.is_dir() and (d / "LGLocalManager.exe").exists()
        ]
        if candidates:
            source_dir = candidates[0]
        else:
            raise RuntimeError("Full zip에서 LGLocalManager.exe를 찾지 못했습니다.")

    preserve_list = ", ".join(f'"{p}"' for p in PRESERVE_PATHS)
    script = _FULL_SWAP_SCRIPT.format(
        pid=os.getpid(),
        source_dir=str(source_dir),
        target_dir=str(app_dir),
        tmp_dir=str(tmp_dir),
        preserve_list=preserve_list,
    )
    _launch_script(script)


def apply_update(info: UpdateInfo, app_dir: Path, on_before_exit=None) -> None:
    """
    info.requires_full 에 따라 Update(exe만) 또는 Full(전체) 설치를 자동 선택해 적용한다.
    적용 스크립트를 띄운 뒤 앱을 종료시킨다.
    """
    logger.info(
        "업데이트 적용: %s -> %s (%s)",
        APP_VERSION,
        info.version,
        "Full" if info.requires_full else "Update",
    )
    if info.requires_full:
        _apply_full_update(info, app_dir)
    else:
        _apply_light_update(info, app_dir)

    if on_before_exit:
        on_before_exit()
