"""Exercise report semantics when the sealed target already contains the updater."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater_repair as repair


def test_assemble_does_not_claim_repair_for_already_fixed_package(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    root = tmp_path / 'repo'
    exe = root / 'worker-client/dist/CheJinUpdater.exe'
    exe.parent.mkdir(parents=True); exe.write_bytes(b'fixed-updater')
    source = root / 'worker-client/packaging/repair-updater.ps1'
    source.parent.mkdir(); source.write_text('REPAIR_DESCRIPTOR_SHA256_PLACEHOLDER')
    area = tmp_path / 'area'; area.mkdir()
    for mode, version in [('old', '0.10.2'), ('target', '0.10.4')]:
        package = area / mode / 'CheJinWorkerClient'; package.mkdir(parents=True)
        (package / 'CheJinUpdater.exe').write_bytes(exe.read_bytes())
        (package / 'update-package-manifest.json').write_text(json.dumps({'version': version}))
    key = Ed25519PrivateKey.generate()
    public_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    trust = json.dumps({'keys': [{'key_id': 'synthetic', 'public_key_base64': base64.b64encode(public_bytes).decode()}]}).encode()
    (area / 'public-keys.json').write_bytes(trust)
    archive = SimpleNamespace(toc={'contracts/c2_contract_v3.json': None, 'PYZ.pyz': None},
        extract=lambda name: trust,
        open_embedded_archive=lambda name: SimpleNamespace(toc={'apps.wechat_ai_customer_service.adapters.client_release_rules': None}))
    env = {'CHEJIN_RELEASE_SIGNING_KEY_ID': 'synthetic', 'CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64':
           base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()}
    with patch.object(repair, 'ROOT', root), patch.dict(os.environ, env), \
         patch.object(repair.subprocess, 'check_output', return_value='a'*40), \
         patch.object(repair.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
         patch.dict(sys.modules, {'PyInstaller': SimpleNamespace(), 'PyInstaller.archive': SimpleNamespace(),
                                 'PyInstaller.archive.readers': SimpleNamespace(CArchiveReader=lambda path: archive)}):
        repair.assemble(SimpleNamespace(root=area, version='0.10.4'))
    report = json.loads((area / 'repair-checks.json').read_text())
    assert 'repair_idempotence_restore_both_versions' not in report
    assert len(report['repair_idempotence_restore']) == 2
    assert all(r['status'] == 'not_needed_already_shipped_same_updater' and not r['repair_executed']
               for r in report['repair_idempotence_restore'])
    assert json.loads((area / 'repair/repair.json').read_text())['baselines'] == []
