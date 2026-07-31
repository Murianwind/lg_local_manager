# PyInstaller 빌드 스펙.
# GitHub Actions에서 `pyinstaller build.spec` 으로 빌드한다.
# runtime/(node, rethink)과 WinDivert 파일, config/는 빌드 스크립트가
# dist/LGLocalManager/ 아래에 별도로 복사해 넣는다 (아래 workflow 참고).

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["app/main.py"],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LGLocalManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=True,  # 실행 시 자동으로 관리자 권한 요청
)
