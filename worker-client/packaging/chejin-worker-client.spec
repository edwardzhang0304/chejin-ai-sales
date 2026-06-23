# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

ROOT = Path.cwd()
OMNIAUTO_RPA_SOURCE = Path(os.environ.get("CHEJIN_OMNIAUTO_RPA_SOURCE") or ROOT / "omniauto-rpa")
OMNIAUTO_SIDECAR = OMNIAUTO_RPA_SOURCE / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"

if not OMNIAUTO_SIDECAR.exists():
    raise SystemExit(f"OmniAuto sidecar not found: {OMNIAUTO_SIDECAR}")

a = Analysis(
    ["chejin_worker_client/main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(OMNIAUTO_RPA_SOURCE), "omniauto-rpa"),
        (str(ROOT / "chejin_worker_client" / "web_assets"), "chejin_worker_client/web_assets"),
    ],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="车金Worker客户端",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="车金Worker客户端",
)
