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

#  같은 코드를 콘솔 버전으로 한 번 더 낸다 — **진단용**.
#  windowed exe는 stdout/stderr가 없어서 `status`·`apply` 같은 명령을 돌려도
#  화면에 아무것도 안 나온다. 윈도우에서 문제가 생겼을 때 로그 파일만 보고
#  짐작해야 했던 이유다. 이쪽은 cmd에서 결과가 바로 보인다.
#
#      jejusched-cli.exe status
#      jejusched-cli.exe apply "C:\\...\\0907_주요일정.hwpx" --dry-run
#      jejusched-cli.exe -v apply "C:\\...\\0907_주요일정.hwpx"
#
#  Analysis를 공유하므로 빌드 시간도 용량도 거의 늘지 않는다.
exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jejusched-cli",
    debug=False,
    strip=False,
    upx=False,
    console=True,       # ← 여기가 유일한 차이
    icon=str(icon) if icon.is_file() else None,
)

coll = COLLECT(
    exe,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="jejusched",
)
