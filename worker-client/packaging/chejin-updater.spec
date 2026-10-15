# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path.cwd()
SHARED_RULES_ROOT = ROOT / "omniauto-rpa"
ENTRY_PATH = ROOT / "packaging" / "chejin_updater_entry.py"
SIGNING_KEYS_PATH = Path(
    os.environ.get("CHEJIN_RELEASE_SIGNING_KEYS_PATH")
    or ROOT / "packaging" / "release-signing-public-keys.json"
)

if not SIGNING_KEYS_PATH.is_file():
    raise SystemExit("release signing public key file is missing")

# Analysis preserves the source basename. The runtime always loads this exact
# name, even when CI provides the public key from a differently named file.
UPDATER_TRUST_PATH = ROOT / "build" / "updater-trust" / "release-signing-public-keys.json"
UPDATER_TRUST_PATH.parent.mkdir(parents=True, exist_ok=True)
UPDATER_TRUST_PATH.write_bytes(SIGNING_KEYS_PATH.read_bytes())

a = Analysis(
    [str(ENTRY_PATH)],
    pathex=[str(ROOT), str(SHARED_RULES_ROOT)],
    binaries=[],
    datas=[
        (str(UPDATER_TRUST_PATH), "."),
        (str(ROOT.parent / "contracts" / "c2_contract_v3.json"), "contracts"),
        (str(ROOT.parent / "contracts" / "hook_contract_v1.json"), "contracts"),
    ],
    # shared_rules.__getattr__ imports these pure adapters dynamically. The
    # standalone one-file updater cannot borrow the Worker's source tree.
    hiddenimports=["cryptography", "psutil", *[
        "apps.wechat_ai_customer_service.adapters." + name
        for name in (
            "contract_rules", "message_contract", "read_settlement", "send_interruption",
            "historical_text_correction", "historical_text_alignment", "text_correspondence",
            "hook_contract_rules", "hook_contact_rules", "hook_history_rules", "hook_event_rules",
            "hook_observability_rules", "client_release_rules",
        )
    ]],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "updater_runtime_hook.py")],
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
