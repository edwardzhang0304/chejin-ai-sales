"""Local package assembly tests, with an explicitly artificial Windows runtime.

These execute the actual copy/ZIP paths and scan every member, but do not claim
that the placeholder python.exe is a Windows build or an accepted release.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
KEY = "FAKE-VISION-0967-DELIVERY-SENTINEL"


@pytest.mark.parametrize("inherited_secret", [False, True])
def test_actual_app_copy_and_zip_never_embed_vision_key(tmp_path, monkeypatch, inherited_secret):
    spec = importlib.util.spec_from_file_location("vision_delivery_builder", ROOT / "scripts/build-fast-uat-package.py")
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    runtime = tmp_path / "artificial-runtime"; runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"ARTIFICIAL-WINDOWS-RUNTIME-NOT-EXECUTABLE")
    (runtime / "pythonw.exe").write_bytes(b"ARTIFICIAL-WINDOWS-RUNTIME-NOT-EXECUTABLE")
    (runtime / "fast-uat-runtime-base.json").write_text(json.dumps({"test_fixture": True}))
    commit = "a" * 40
    # Only the release-admission identity is synthetic. Actual Worker/OmniAuto
    # source selection, copy functions, manifest and ZIP assembly run unchanged.
    monkeypatch.setattr(builder, "verify_build_source", lambda *a, **kw: {"git_commit": commit, "git_dirty": False})
    if inherited_secret: monkeypatch.setenv("CHEJIN_VISION_CLIENT_API_KEY", KEY)
    else: monkeypatch.delenv("CHEJIN_VISION_CLIENT_API_KEY", raising=False)
    result = builder.build(runtime_root=runtime, output_dir=tmp_path / "assembly", git_commit=commit, git_branch="synthetic-test-identity")
    with zipfile.ZipFile(result["zip_path"]) as archive:
        names = archive.namelist()
        assert len(names) > 100  # real app selection, not a manifest-only fixture
        for name in names:
            assert not name.endswith("vision-runtime.json")
            assert KEY.encode() not in archive.read(name), name
        manifest = json.loads(archive.read("CheJinWorkerDebug/fast-uat-manifest.json"))
        assert manifest["vision_credential_embedded"] is False
        assert manifest["vision_credential_source"] == "worker_backend"
        unpacked = tmp_path / "unpacked"
        archive.extractall(unpacked)
    for path in unpacked.rglob("*"):
        if path.is_file(): assert KEY.encode() not in path.read_bytes(), path.name
    evidence = os.environ.get("CHEJIN_VISION_EVIDENCE_DIR")
    if evidence:
        destination = Path(evidence); destination.mkdir(parents=True, exist_ok=True)
        (destination / f"assembly-secret-{inherited_secret}.json").write_text(json.dumps({"artificial_windows_runtime": True, "synthetic_source_identity": True, "production_copy_and_zip_code": True, "full_members_scanned": len(names), "inherited_fake_secret": inherited_secret, "leaks": 0, "windows_package_acceptance": False}, indent=2))
