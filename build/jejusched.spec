# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 스펙 — 설계서 §10.

onedir + windowed. OAuth 클라이언트 JSON은 빌드 시 GitHub Secret에서
`build/client_secret.json`으로 기록되어 여기에 함께 실린다(저장소에는 없다).
"""

from pathlib import Path

SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent

datas = []
client_secret = SPEC_DIR / "client_secret.json"
if client_secret.is_file():
    datas.append((str(client_secret), "."))

icon = SPEC_DIR / "jejusched.ico"

a = Analysis(
    [str(ROOT / "jejusched" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "win32crypt",
        "win32timezone",
        #  자동 실행 바로가기를 만드는 COM 경로 (jejusched/autostart.py)
        "win32com.client",
        "pythoncom",
        "pywintypes",
        "windows_toasts",
        "google.genai",
        "googleapiclient.discovery",
        "pystray._win32",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pymupdf", "fitz", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jejusched",
    debug=False,
    strip=False,
    upx=False,          # UPX는 백신 오탐을 늘린다
    console=False,      # 트레이 상주 앱 — 콘솔 창을 띄우지 않는다
    icon=str(icon) if icon.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="jejusched",
)
