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

import json
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

# 교체 스크립트(PowerShell, 완전히 분리된 프로세스라 우리 로거로 못 남김)가
# 자기 진행 상황을 남기는 파일 이름. data/ 밑에 두면 exe 교체와 무관하게 남는다.
UPDATE_APPLY_LOG_FILENAME = "update-apply.log"

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
        expected_size = r.headers.get("Content-Length")
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                written += len(chunk)

    # 다운로드가 중간에 끊겼는데도 조용히 "성공"으로 넘어가면, 그 잘린 zip이
    # 손상된 exe를 만들어내는 원인이 될 수 있다 — 여기서 미리 잡아낸다.
    if expected_size is not None and written != int(expected_size):
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"다운로드가 불완전합니다: {written} / {expected_size} 바이트만 받음 ({url})"
        )


_LIGHT_SWAP_SCRIPT = r'''
$logFile = "{log_file}"
function Log($msg) {{
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $logFile -Append -Encoding utf8
}}

try {{
    $pid_to_wait = {pid}
    $newExe = "{new_exe}"
    $targetExe = "{target_exe}"
    $tmpDir = "{tmp_dir}"

    Log "update start (light): pid=$pid_to_wait -> $targetExe"

    $count = 0
    while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
        Start-Sleep -Milliseconds 500
        $count++
    }}
    Log "old process exited (waited $count x 0.5s)"

    # 프로세스가 막 종료된 직후엔 OS/백신이 exe 파일을 잠깐 더 붙잡고 있을 수
    # 있어서, 한 번 실패해도 바로 포기하지 않고 몇 초간 재시도한다.
    #
    # 기존 파일에 바로 덮어쓰지 않고 .new 임시 이름으로 먼저 전부 복사한 뒤,
    # 크기까지 맞는 걸 확인하고 나서야 진짜 파일 이름으로 바꿔치기한다 —
    # 복사 도중에 뭔가 끼어들어도(백신 스캔 등) 기존 exe는 안전하게 남아있고,
    # "절반만 써진 exe"가 실제 파일명으로 남는 사고를 막는다.
    $stagingExe = "$targetExe.new"
    $copied = $false
    for ($i = 0; $i -lt 10; $i++) {{
        try {{
            Copy-Item -Force $newExe $stagingExe
            $sourceSize = (Get-Item $newExe).Length
            $stagedSize = (Get-Item $stagingExe).Length
            if ($sourceSize -ne $stagedSize) {{
                throw "size mismatch: source=$sourceSize staged=$stagedSize"
            }}
            $copied = $true
            break
        }} catch {{
            Log "copy attempt $i failed: $_"
            Remove-Item -Force $stagingExe -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }}
    }}
    if (-not $copied) {{
        Log "ERROR: could not replace exe after retries, giving up"
        exit 1
    }}
    Move-Item -Force $stagingExe $targetExe
    Log "exe replaced"

    Start-Process -FilePath $targetExe -ArgumentList "--from-auto-update"
    Log "relaunched: $targetExe --from-auto-update"

    Start-Sleep -Seconds 2
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    Log "update done (light)"
}} catch {{
    Log "FATAL: $_"
}}
'''

_FULL_SWAP_SCRIPT = r'''
$logFile = "{log_file}"
function Log($msg) {{
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $logFile -Append -Encoding utf8
}}

try {{
    $pid_to_wait = {pid}
    $sourceDir = "{source_dir}"
    $targetDir = "{target_dir}"
    $tmpDir = "{tmp_dir}"
    $exeName = "LGLocalManager.exe"
    $preserve = @({preserve_list})

    Log "update start (full): pid=$pid_to_wait -> $targetDir"

    $count = 0
    while ((Get-Process -Id $pid_to_wait -ErrorAction SilentlyContinue) -and ($count -lt 60)) {{
        Start-Sleep -Milliseconds 500
        $count++
    }}
    Log "old process exited (waited $count x 0.5s)"

    Get-ChildItem -Path $sourceDir -Force | ForEach-Object {{
        if ($preserve -contains $_.Name) {{
            return
        }}
        $destPath = Join-Path $targetDir $_.Name
        $copied = $false
        for ($i = 0; $i -lt 10; $i++) {{
            try {{
                if (Test-Path $destPath) {{
                    Remove-Item -Recurse -Force $destPath
                }}
                Copy-Item -Recurse -Force $_.FullName $destPath
                $copied = $true
                break
            }} catch {{
                Log "copy attempt $i failed for $($_.Name): $_"
                Start-Sleep -Seconds 1
            }}
        }}
        if (-not $copied) {{
            Log "ERROR: could not replace $($_.Name) after retries"
        }}
    }}
    Log "files replaced"

    Start-Process -FilePath (Join-Path $targetDir $exeName) -ArgumentList "--from-auto-update"
    Log "relaunched: $(Join-Path $targetDir $exeName) --from-auto-update"

    Start-Sleep -Seconds 2
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    Log "update done (full)"
}} catch {{
    Log "FATAL: $_"
}}
'''


def _launch_script(script_content: str, app_dir: Path) -> None:
    script_path = Path(tempfile.gettempdir()) / f"lglocalmanager_apply_{os.getpid()}.ps1"
    script_path.write_text(script_content, encoding="utf-8")

    # 스크립트 자신이 남기는 로그(update-apply.log)는 스크립트가 최소한
    # 시작은 해야 의미가 있다 — 파싱 자체가 실패하면(예: 이스케이프 실수로
    # PowerShell 문법이 깨짐) 아무것도 안 남는다. 그래서 여기서 Python이
    # PowerShell 프로세스의 stdout/stderr를 직접 파일로 강제 리다이렉트해서,
    # 스크립트가 시작도 못 하고 죽는 경우까지 잡아낸다.
    stderr_path = app_dir / "data" / "update-apply-stderr.log"
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("교체 스크립트 파일: %s", script_path)
    logger.info("교체 스크립트 stdout/stderr 로그: %s", stderr_path)

    with open(stderr_path, "w", encoding="utf-8") as stderr_file:
        subprocess.Popen(
            [
                "powershell",
                "-ExecutionPolicy", "Bypass",
                "-File", str(script_path),
            ],
            stdout=stderr_file,
            stderr=subprocess.STDOUT,
            # 이전엔 DETACHED_PROCESS(콘솔 자체가 없음) + "-WindowStyle Hidden"
            # (콘솔 창을 숨겨라)를 같이 썼는데, 존재하지도 않는 창을 숨기라고
            # 시키니 PowerShell이 시작 단계에서 조용히 죽어버렸다 — 로그를
            # 한 줄도 못 남기고 사라진 원인이 이거였다. CREATE_NO_WINDOW는
            # "애초에 콘솔 창을 안 만드는" 방식이라 이 충돌이 없다.
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )


def _download_and_extract(url: str, timeout: float = 120.0) -> tuple[Path, Path]:
    """zip을 임시 폴더에 받아 풀고, (임시 폴더, 압축 해제 폴더) 를 돌려준다.

    Light/Full 업데이트 둘 다 "받아서 풀기"까지는 완전히 동일한 절차라 여기
    하나로 모았다 — 이후 "그 안에서 exe를 어디까지 교체할지"만 서로 다르다.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="lglocalmanager-update-"))
    zip_path = tmp_dir / "update.zip"
    extract_dir = tmp_dir / "extracted"

    _download(url, zip_path, timeout=timeout)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    return tmp_dir, extract_dir


def _find_exe_dir(extract_dir: Path) -> Path:
    """LGLocalManager.exe가 들어 있는 폴더를 찾는다.

    zip 안에 폴더 한 겹이 더 있을 수도, 없을 수도 있어서 두 경우 다 본다.
    """
    if (extract_dir / "LGLocalManager.exe").exists():
        return extract_dir

    candidates = [
        d for d in extract_dir.iterdir() if d.is_dir() and (d / "LGLocalManager.exe").exists()
    ]
    if candidates:
        return candidates[0]

    raise RuntimeError("압축 해제한 파일에서 LGLocalManager.exe를 찾지 못했습니다.")


def _apply_light_update(info: UpdateInfo, app_dir: Path) -> None:
    if info.update_zip_url is None:
        raise ValueError("Update zip URL이 없는 UpdateInfo로 light update를 시도했습니다.")

    logger.info("Update(exe만) 다운로드: %s", info.update_zip_url)
    tmp_dir, extract_dir = _download_and_extract(info.update_zip_url)
    exe_dir = _find_exe_dir(extract_dir)
    log_file = app_dir / "data" / UPDATE_APPLY_LOG_FILENAME
    logger.info("교체 스크립트 로그 위치: %s", log_file)

    script = _LIGHT_SWAP_SCRIPT.format(
        pid=os.getpid(),
        new_exe=str(exe_dir / "LGLocalManager.exe"),
        target_exe=str(app_dir / "LGLocalManager.exe"),
        tmp_dir=str(tmp_dir),
        log_file=str(log_file),
    )
    _launch_script(script, app_dir)


def _apply_full_update(info: UpdateInfo, app_dir: Path) -> None:
    if info.full_zip_url is None:
        raise ValueError("Full zip URL이 없는 UpdateInfo로 full update를 시도했습니다.")

    logger.info("Full(전체) 다운로드: %s", info.full_zip_url)
    tmp_dir, extract_dir = _download_and_extract(info.full_zip_url)
    source_dir = _find_exe_dir(extract_dir)
    log_file = app_dir / "data" / UPDATE_APPLY_LOG_FILENAME
    logger.info("교체 스크립트 로그 위치: %s", log_file)

    preserve_list = ", ".join(f'"{p}"' for p in PRESERVE_PATHS)
    script = _FULL_SWAP_SCRIPT.format(
        pid=os.getpid(),
        source_dir=str(source_dir),
        target_dir=str(app_dir),
        tmp_dir=str(tmp_dir),
        preserve_list=preserve_list,
        log_file=str(log_file),
    )
    _launch_script(script, app_dir)


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
