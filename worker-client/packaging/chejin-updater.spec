# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path.cwd()
ENTRY_PATH = ROOT / "packaging" / "chejin_updater_entry.py"
SIGNING_KEYS_PATH = Path(
    os.environ.get("CHEJIN_RELEASE_SIGNING_KEYS_PATH")
    or ROOT / "packaging" / "release-signing-public-keys.json"
)

if not SIGNING_KEYS_PATH.is_file():
    raise SystemExit("release signing public key file is missing")

a = Analysis(
    [str(ENTRY_PATH)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(SIGNING_KEYS_PATH), ".")],
    hiddenimports=["cryptography", "psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CheJinUpdater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=True,
)
