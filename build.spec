# PyInstaller 빌드 스펙.
# GitHub Actions에서 `pyinstaller build.spec` 으로 빌드한다.
# runtime/(node, rethink)과 WinDivert 파일, config/는 빌드 스크립트가
# dist/LGLocalManager/ 아래에 별도로 복사해 넣는다 (아래 workflow 참고).
#
# onefile이 아니라 onedir 방식이다 — onefile은 실행할 때마다 %TEMP%에 내부
# DLL들을 압축 해제하고 그걸로 구동하는데, 이 과정이 원인 불명의 이유로
# ("Failed to load Python DLL" 에러) 자주 실패하는 문제가 있었다. onedir은
# 그 파일들을 배포 시점에 미리 풀어놓은 채로 두기 때문에, 실행할 때마다
# 압축을 푸는 단계 자체가 없어져서 이 문제가 구조적으로 사라진다.
# (대신 LGLocalManager.exe 옆에 _internal/ 폴더가 같이 있어야 한다 —
# build.yml/updater.py도 이 구조에 맞춰져 있다.)

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LGLocalManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=True,  # 실행 시 자동으로 관리자 권한 요청
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="LGLocalManager",
)
