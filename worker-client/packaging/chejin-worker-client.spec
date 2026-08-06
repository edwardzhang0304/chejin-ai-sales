# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "scripts"))
from build_source import resolve_contract_path
from client_delivery_policy import (
    is_client_forbidden_path,
    load_client_exclude_paths,
)

OMNIAUTO_RPA_SOURCE = Path(os.environ.get("CHEJIN_OMNIAUTO_RPA_SOURCE") or ROOT / "omniauto-rpa")
OMNIAUTO_SIDECAR = OMNIAUTO_RPA_SOURCE / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
CONTRACT_PATH = resolve_contract_path(ROOT)
BUILD_IDENTITY_PATH = Path(
    os.environ.get("CHEJIN_BUILD_IDENTITY_PATH") or ""
)

if not OMNIAUTO_SIDECAR.exists():
    raise SystemExit(f"OmniAuto sidecar not found: {OMNIAUTO_SIDECAR}")
if not CONTRACT_PATH.exists():
    raise SystemExit(f"C2 contract not found: {CONTRACT_PATH}")

EXCLUDED_OMNIAUTO_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "cache",
    "runtime",
    "build",
    "dist",
}
ALLOWED_OMNIAUTO_DATA_PREFIXES = (
    "apps/wechat_ai_customer_service/data/tenants/chejin/product_master/",
    "apps/wechat_ai_customer_service/data/tenants/chejin/rag_index/",
)
OMNIAUTO_CLIENT_EXCLUDES = load_client_exclude_paths(OMNIAUTO_RPA_SOURCE)


def include_omniauto_file(path):
    rel = path.relative_to(OMNIAUTO_RPA_SOURCE)
    rel_name = rel.as_posix()
    if is_client_forbidden_path(rel_name, OMNIAUTO_CLIENT_EXCLUDES):
        return False
    if any(part in EXCLUDED_OMNIAUTO_PARTS for part in rel.parts):
        return False
    if "data" in rel.parts and not any(
        rel_name.startswith(prefix) for prefix in ALLOWED_OMNIAUTO_DATA_PREFIXES
    ):
        return False
    if path.suffix in {".pyc", ".pyo", ".zip", ".env"}:
        return False
    if path.name == ".env" or path.name.endswith(".local.env"):
        return False
    return True


OMNIAUTO_DATAS = [
    (
        str(path),
        "omniauto-rpa/" + path.relative_to(OMNIAUTO_RPA_SOURCE).parent.as_posix(),
    )
    for path in OMNIAUTO_RPA_SOURCE.rglob("*")
    if path.is_file() and include_omniauto_file(path)
]

a = Analysis(
    [str(ROOT / "chejin_worker_client" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        *OMNIAUTO_DATAS,
        (str(CONTRACT_PATH), "contracts"),
        (str(ROOT / "chejin_worker_client" / "web_assets"), "chejin_worker_client/web_assets"),
        *(
            [(str(BUILD_IDENTITY_PATH), ".")]
            if BUILD_IDENTITY_PATH.is_file()
            else []
        ),
    ],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "uiautomation",
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
