import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ops/formal_release"))
sys.path.insert(0, str(ROOT / "worker-client"))
import deliver
import quick_gate
import validate_dispatch
import configure_github
import receiver
import select_artifact
from verify import CHUNK, digest, verify
from chejin_worker_client import __version__ as CURRENT_VERSION
TARGET_VERSION = ".".join([*CURRENT_VERSION.split(".")[:2], str(int(CURRENT_VERSION.split(".")[2]) + 1)])
from chejin_worker_client.models import ClientRelease
from chejin_worker_client.release_package_contract import canonical_release_manifest


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "source"
        self.folder.mkdir()
        self.key = Ed25519PrivateKey.generate()
        public = self.root / "keys.json"
        public.write_text(json.dumps({"keys": [{"key_id": "fixture", "algorithm": "ed25519", "public_key_base64":
            base64.b64encode(self.key.public_key().public_bytes_raw()).decode()}]}))
        self.config = {"staging_root": str(self.root / "stage"), "public_keys": str(public),
                       "client_baselines": {CURRENT_VERSION: str(ROOT / "worker-client")}, "api_origin": "https://example.test/api"}
        self.stem = f"chejin-worker-v{TARGET_VERSION}-windows-x64"
        self.files = {"CheJinWorkerClient.exe": b"fixture client", "CheJinUpdater.exe": b"fixture updater",
                      "_internal/contracts/c2_contract_v3.json": json.dumps({"contract_revision": TARGET_VERSION}).encode(),
                      "_internal/fixture.bin": b"0" * (CHUNK + 32)}
        self.make_artifact()

    def make_artifact(self, extra=None):
        sha = lambda b: hashlib.sha256(b).hexdigest()
        manifest = {"schema_version": 1, "version": TARGET_VERSION, "platform": "windows-x64", "git_commit": "a" * 40,
                    "rollback_safe": True, "files": {name: sha(body) for name, body in self.files.items()}}
        manifest_raw = json.dumps(manifest).encode()
        archive = self.folder / (self.stem + ".zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("CheJinWorkerClient\\_internal\\", b"")
            for name, body in self.files.items():
                z.writestr("CheJinWorkerClient/" + name, body)
            z.writestr("CheJinWorkerClient/update-package-manifest.json", manifest_raw)
            if extra:
                z.writestr(*extra)
        self.desc = {"version": TARGET_VERSION, "channel": "gray", "platform": "windows-x64", "status": "published",
            "git_commit": "a" * 40, "artifact_sha256": digest(archive), "artifact_size_bytes": archive.stat().st_size,
            "artifact_storage_key": "gray/windows-x64/" + archive.name, "package_manifest_sha256": sha(manifest_raw),
            "published_at": "2026-09-07T00:00:00Z", "minimum_updater_version": "0.9.59", "rollback_safe": True,
            "signature_key_id": "fixture", "release_notes": ""}
        release = ClientRelease.from_api({**self.desc, "latest_version": TARGET_VERSION, "update_available": True})
        self.desc["manifest_signature"] = base64.b64encode(self.key.sign(canonical_release_manifest(release))).decode()
        (self.folder / (self.stem + ".release.json")).write_text(json.dumps(self.desc))
        delivery = {"version": TARGET_VERSION, "build_commit": "a" * 40, "workflow_run_id": "123", "zip_sha256": digest(archive),
            "upgrade_start_version": CURRENT_VERSION, "original_client_upgrade_check": "passed",
            "original_client_upgrade_report_sha256": "f" * 64,
            "default_api_base_url": "https://example.test/api", "tests_status": "passed", "preflight_status": "passed",
            "vision_credential_embedded": False, "vision_credential_source": "worker_backend", "vision_configuration_locked": True,
            "vision_live_probe_check": "runtime_after_binding", "c2_contract_revision": TARGET_VERSION,
            "exe_sha256": sha(self.files["CheJinWorkerClient.exe"]), "updater_exe_sha256": sha(self.files["CheJinUpdater.exe"]),
            "c2_contract_sha256": sha(self.files["_internal/contracts/c2_contract_v3.json"])}
        (self.folder / (self.stem + ".delivery.json")).write_text(json.dumps(delivery))
        (self.folder / (self.stem + ".sha256.txt")).write_text(digest(archive) + "  " + archive.name + "\n")
        self.meta, _ = deliver.metadata(self.folder, CURRENT_VERSION, "123", "a" * 40)

    def test_baseline_identifies_client_version_separately_from_updater_protocol(self):
        from verify import client_api
        from chejin_worker_client import release_package_contract
        _, verifier = client_api(self.config, CURRENT_VERSION)
        self.assertEqual(verifier.UPDATER_VERSION, release_package_contract.UPDATER_VERSION)
        wrong = {**self.config, "client_baselines": {"0.9.60": str(ROOT / "worker-client")}}
        with self.assertRaisesRegex(ValueError, "OLD_CLIENT_BASELINE_MISMATCH"):
            client_api(wrong, "0.9.60")

    def test_missing_real_client_upgrade_evidence_blocks_staging(self):
        path = self.folder / (self.stem + ".delivery.json")
        payload = json.loads(path.read_text())
        payload.pop("original_client_upgrade_check")
        path.write_text(json.dumps(payload))
        meta, _ = deliver.metadata(self.folder, CURRENT_VERSION, "123", "a" * 40)
        with self.assertRaisesRegex(ValueError, "ORIGINAL_CLIENT_UPGRADE_GATE_FAILED"):
            verify(self.folder, meta, self.config)

    def remote(self, request, body=b"", role="stage"):
        with patch("receiver.shutil.disk_usage") as space:
            space.return_value.free = 20 * 1024 ** 3
            return receiver.handle(request, io.BytesIO(body), role, self.config)

    def test_client_only_release_uses_signed_contract_revision(self):
        old_revision = "0.9.68"
        self.files["_internal/contracts/c2_contract_v3.json"] = json.dumps({"contract_revision": old_revision}).encode()
        self.make_artifact()
        path = self.folder / (self.stem + ".delivery.json")
        delivery = json.loads(path.read_text())
        delivery["c2_contract_revision"] = old_revision
        path.write_text(json.dumps(delivery))
        self.meta, _ = deliver.metadata(self.folder, CURRENT_VERSION, "123", "a" * 40)
        result = deliver.stage(self.folder, self.meta, self.desc, self.remote)
        self.assertEqual(result["contract_revision"], old_revision)
        delivery["c2_contract_revision"] = "0.0.1"
        path.write_text(json.dumps(delivery))
        self.meta, _ = deliver.metadata(self.folder, CURRENT_VERSION, "123", "a" * 40)
        with self.assertRaisesRegex(ValueError, "CONTRACT_REVISION_MISMATCH"):
            deliver.stage(self.folder, self.meta, self.desc, self.remote)

    def test_real_signed_delivery_idempotent_and_immutable(self):
        first = deliver.stage(self.folder, self.meta, self.desc, self.remote)
        self.assertEqual(first["package"], "passed")
        self.assertGreater(first["sent_chunks"], 1)
        second = deliver.stage(self.folder, self.meta, self.desc, self.remote)
        self.assertEqual(second["sent_chunks"], 0)
        self.assertEqual(second["sha256"], first["sha256"])
        self.files["CheJinWorkerClient.exe"] = b"replacement"
        self.make_artifact()
        with self.assertRaisesRegex(ValueError, "IMMUTABLE_VERSION_CONFLICT"):
            deliver.stage(self.folder, self.meta, self.desc, self.remote)

    def test_interrupted_transfer_resumes_existing_chunks(self):
        count = 0
        def interrupted(request, body=b""):
            nonlocal count
            if request["operation"] == "chunk":
                count += 1
                if count == 2:
                    raise ConnectionError("test disconnect")
            return self.remote(request, body)
        with self.assertRaises(ConnectionError):
            deliver.stage(self.folder, self.meta, self.desc, interrupted)
        result = deliver.stage(self.folder, self.meta, self.desc, self.remote)
        self.assertEqual(result["reused_chunks"], 1)
        self.assertEqual(result["package"], "passed")

    def test_receiver_process_protocol_and_ignored_shell_command(self):
        import os
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(self.config))
        escaped = self.root / 'shell-was-executed'
        command = [sys.executable, '-c',
                   "import sys;from pathlib import Path;import receiver;receiver.CONFIG=Path(sys.argv[1]);sys.argv=['receiver','stage'];receiver.main()",
                   str(config_path)]
        def remote(request, body=b''):
            completed = subprocess.run(command, input=json.dumps(request).encode()+b'\n'+body, capture_output=True,
                cwd=ROOT/'ops/formal_release', env={**os.environ,'SSH_ORIGINAL_COMMAND':'touch '+str(escaped)}, timeout=60)
            self.assertEqual(completed.returncode,0,completed.stderr.decode())
            return json.loads(completed.stdout)
        result = deliver.stage(self.folder,self.meta,self.desc,remote)
        self.assertEqual(result['package'],'passed')
        self.assertFalse(escaped.exists())
        self.assertTrue((Path(self.config['staging_root'])/'audit.jsonl').is_file())

    def test_corrupt_chunk_retransmitted(self):
        token = self.remote({"operation": "init", "metadata": self.meta, "descriptor": self.desc})["stage_id"]
        (Path(self.config["staging_root"]) / token / (self.stem + ".zip.part0")).write_bytes(b"bad")
        result = deliver.stage(self.folder, self.meta, self.desc, self.remote)
        self.assertEqual(result["reused_chunks"], 0)
        self.assertEqual(result["package"], "passed")

    def test_unsigned_or_unknown_key_cannot_reserve_stage(self):
        for change in ({"manifest_signature": "invalid"}, {"signature_key_id": "untrusted"}):
            with self.assertRaises(Exception):
                self.remote({"operation": "init", "metadata": self.meta, "descriptor": {**self.desc, **change}})
        self.assertFalse(list((self.root / "stage").glob("*/metadata.json")))

    def test_path_traversal_and_extra_unmanifested_file_blocked(self):
        for name in ("CheJinWorkerClient/../../escaped", "CheJinWorkerClient/extra.txt", "Other/file"):
            self.make_artifact((name, b"test"))
            with self.assertRaises(Exception):
                verify(self.folder, self.meta, self.config)
        self.assertFalse((self.root / "escaped").exists())

    def test_packaged_secret_and_state_blocked(self):
        for name, body in ((".env", b"fixture"), ("data.sqlite", b"fixture"), ("secret.txt", b"sk-" + b"x" * 40)):
            self.make_artifact(("CheJinWorkerClient/" + name, body))
            with self.assertRaisesRegex(ValueError, "PACKAGED_SECRET"):
                verify(self.folder, self.meta, self.config)

    def test_stage_key_never_publishes_and_unsealed_stage_cannot_publish(self):
        token = self.remote({"operation": "init", "metadata": self.meta, "descriptor": self.desc})["stage_id"]
        with patch("receiver.publish") as publish:
            with self.assertRaisesRegex(ValueError, "ROLE_DENIED"):
                self.remote({"operation": "publish", "stage_id": token})
            with self.assertRaisesRegex(ValueError, "STAGE_NOT_VERIFIED"):
                self.remote({"operation": "publish", "stage_id": token, "workers_drained": True, "current_version": CURRENT_VERSION}, role="promote")
            publish.assert_not_called()

    def test_queue_confirmation_required_and_publish_failure_not_success(self):
        token = deliver.stage(self.folder, self.meta, self.desc, self.remote)["stage_id"]
        with patch("receiver.publish", side_effect=ValueError("BACKEND_CONTRACT_MISMATCH")) as publish:
            with self.assertRaisesRegex(ValueError, "LOCAL_QUEUE_CONFIRMATION_REQUIRED"):
                self.remote({"operation": "publish", "stage_id": token}, role="promote")
            publish.assert_not_called()
            with self.assertRaisesRegex(ValueError, "BACKEND_CONTRACT_MISMATCH"):
                self.remote({"operation": "publish", "stage_id": token, "workers_drained": True, "current_version": CURRENT_VERSION}, role="promote")
        self.assertFalse((Path(self.config["staging_root"]) / token / "publication-result.json").exists())

    def test_unknown_baseline_and_changed_source_blocked(self):
        for field, value in (("current_version", "0.9.60"), ("commit", "b" * 40), ("version", "../../tmp")):
            with self.assertRaises(Exception):
                self.remote({"operation": "init", "metadata": {**self.meta, field: value}, "descriptor": self.desc})

    def test_chunk_range_and_hash_blocked(self):
        token = self.remote({"operation": "init", "metadata": self.meta, "descriptor": self.desc})["stage_id"]
        for index in (-1, 999):
            with self.assertRaises(ValueError):
                self.remote({"operation": "chunk", "stage_id": token, "name": self.stem + ".zip", "index": index, "sha256": "0" * 64}, b"bad")
        with self.assertRaisesRegex(ValueError, "CHUNK_HASH_MISMATCH"):
            self.remote({"operation": "chunk", "stage_id": token, "name": self.stem + ".zip", "index": 0, "sha256": "0" * 64}, b"bad")


class GateTests(unittest.TestCase):
    def test_github_keys_use_stdin_and_separate_environments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('stage','promote','known'):
                (root/name).write_text(name+'-fixture-value')
            config = {'host':'example.test','port':22,'known_hosts_file':str(root/'known'),
                      'stage_key_file':str(root/'stage'),'promote_key_file':str(root/'promote'),
                      'api_origin':'https://api.test/api','download_origin':'https://downloads.test'}
            calls=[]
            def call(args,payload=None):
                calls.append((args,payload))
                return {'branch_policies':[]} if args[0]=='api' and args[-1].endswith('deployment-branch-policies') else None
            with patch('configure_github.call',side_effect=call):
                configure_github.configure('example/repository',config)
            key_calls=[(args,payload) for args,payload in calls if args[:3]==['secret','set','FORMAL_SSH_KEY']]
            self.assertEqual(len(key_calls),2)
            self.assertEqual(key_calls[0][1],'stage-fixture-value')
            self.assertEqual(key_calls[1][1],'promote-fixture-value')
            self.assertNotIn('stage-fixture-value',str(key_calls[0][0]))
            self.assertIn('formal-staging',key_calls[0][0])
            self.assertIn('formal-production',key_calls[1][0])

    def test_missing_receiver_or_approval_blocks_before_build(self):
        valid = {'GITHUB_REF':'refs/heads/codex/gray-release-0.9.x','RELEASE_APPROVED':'true',
                 'RELEASE_REASON':'fixture','DELIVERY_MODE':'build_and_stage','CURRENT_VERSION':'0.9.71',
                 'FORMAL_SSH_KEY':'fixture','FORMAL_SSH_HOST':'example.test','FORMAL_SSH_PORT':'22','FORMAL_KNOWN_HOSTS':'fixture'}
        validate_dispatch.validate(valid)
        for old_version in ('0.9.67', '0.9.68'):
            with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_UPGRADE_START'):
                validate_dispatch.validate({**valid, 'CURRENT_VERSION': old_version})
        for change in ({'FORMAL_SSH_KEY':''},{'RELEASE_APPROVED':'false'},{'GITHUB_REF':'refs/heads/untrusted'},
                       {'DELIVERY_MODE':'publish_staged','STAGE_ID':'a'*64,'PRODUCTION_READY':'false'}):
            with self.assertRaises(ValueError):
                validate_dispatch.validate({**valid,**change})

    def test_fail_fast_stops_before_following_checks(self):
        for failure_at in range(3):
            with self.subTest(failure_at=failure_at), patch("quick_gate.subprocess.run") as run:
                run.side_effect = [subprocess.CompletedProcess([], 0)] * failure_at + [subprocess.CompletedProcess([], 3)]
                self.assertEqual(quick_gate.main(), 3)
                self.assertEqual(run.call_count, failure_at + 1)

    def test_fast_uat_and_failed_formal_job_are_never_selected(self):
        run = {"path": ".github/workflows/worker-windows-package.yml", "event": "workflow_dispatch",
               "head_branch": "codex/gray-release-0.9.x", "head_sha": "a" * 40}
        artifact = {"id": 1, "name": "chejin-worker-v0.9.69-windows-x64-" + "a" * 40, "expired": False}
        self.assertEqual(select_artifact.select(run, [{"name": "package", "conclusion": "success"}], [artifact])[0], 1)
        for path, conclusion in ((".github/workflows/worker-windows-fast-uat.yml", "success"), (run["path"], "failure")):
            with self.assertRaises(ValueError):
                select_artifact.select({**run, "path": path}, [{"name": "package", "conclusion": conclusion}], [artifact])

    def test_retry_transport_only_and_redact_remote_error(self):
        with patch.dict("os.environ", {"FORMAL_SSH_KEY": "fixture", "FORMAL_KNOWN_HOSTS": "fixture", "FORMAL_SSH_HOST": "example.test", "FORMAL_SSH_PORT": "22"}):
            remote = deliver.Remote("stage")
            self.addCleanup(remote.close)
            with patch("deliver.subprocess.run") as run, patch("deliver.time.sleep"):
                run.side_effect = [subprocess.CompletedProcess([], 255, b"", b"private"),
                                   subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")]
                self.assertTrue(remote({"operation": "status"})["ok"])
                self.assertEqual(run.call_count, 2)
            with patch("deliver.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b'{"ok":false,"error":"ROLE_DENIED"}', b"private")) as run:
                with self.assertRaisesRegex(ValueError, "REMOTE_REJECTED_ROLE_DENIED"):
                    remote({"operation": "publish"})
                self.assertEqual(run.call_count, 1)


class ExternalTests(unittest.TestCase):
    def test_full_download_and_ranges_and_identity(self):
        from urllib.parse import urlparse, parse_qs
        body = b"a" * 32 + b"z" * 32
        result = {"version": TARGET_VERSION, "commit": "a" * 40, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
        class Response(io.BytesIO):
            def __init__(self, payload, status=200, headers=None):
                super().__init__(payload)
                self.status, self.headers = status, headers or {}
        class Opener:
            corrupt_range = False
            wrong_host = False
            def open(self, req, timeout):
                url = req if isinstance(req, str) else req.full_url
                if 'client-releases/latest' in url:
                    current = parse_qs(urlparse(url).query)['current_version'][0]
                    payload = {"data": {"update_available": current != TARGET_VERSION, "latest_version": TARGET_VERSION,
                        "artifact_sha256": result['sha256'], "git_commit": result['commit'], "artifact_size_bytes": len(body),
                        "artifact_url": 'https://' + ('evil.test' if self.wrong_host else 'downloads.test') + '/file?token=private'}}
                    return Response(json.dumps(payload).encode())
                if url.endswith(('healthz','readyz')):
                    return Response(b'{}')
                if not isinstance(req, str):
                    span = req.get_header('Range').removeprefix('bytes=')
                    start,end = map(int,span.split('-'))
                    return Response(b'x'*32 if self.corrupt_range else body[start:end+1],206,{'Content-Range':f'bytes {span}/{len(body)}'})
                return Response(body,headers={'Content-Length':str(len(body))})
        opener=Opener()
        with patch('deliver.build_opener',return_value=opener):
            self.assertEqual(deliver.verify_external(result,CURRENT_VERSION,'https://api.test/api','https://downloads.test')['external_download'],'passed')
            opener.corrupt_range=True
            with self.assertRaisesRegex(ValueError,'RANGE_BYTES_MISMATCH'):
                deliver.verify_external(result,CURRENT_VERSION,'https://api.test/api','https://downloads.test')
            opener.wrong_host=True
            with self.assertRaisesRegex(ValueError,'DOWNLOAD_ORIGIN_MISMATCH'):
                deliver.verify_external(result,CURRENT_VERSION,'https://api.test/api','https://downloads.test')


class RegistrationTests(unittest.TestCase):
    def test_contract_idle_transaction_and_check_only_boundaries(self):
        import contextlib
        import runpy
        import types
        from unittest.mock import Mock
        for contract, active, operation, expected_error in (
            ('wrong',False,'publish','BACKEND_CONTRACT_MISMATCH'),
            (TARGET_VERSION,True,'publish','WORKERS_NOT_DRAINED'),
            (TARGET_VERSION,False,'check',None),
            (TARGET_VERSION,False,'publish',None),
        ):
            with self.subTest(contract=contract,active=active,operation=operation), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp)/'release.json').write_text('{}')
                db=Mock();db.scalar.return_value=active
                transaction=Mock()
                @contextlib.contextmanager
                def begin():
                    try:
                        yield db
                    except Exception:
                        transaction.rollback()
                        raise
                    else:
                        transaction.commit()
                registry=Mock();storage=Mock()
                modules={
                    'sqlalchemy':types.SimpleNamespace(text=lambda s:s),
                    'app.contracts.c2':types.SimpleNamespace(contract_revision=lambda:contract,contract_sha256=lambda:'f'*64),
                    'app.core.database':types.SimpleNamespace(SessionLocal=types.SimpleNamespace(begin=begin)),
                    'app.services.client_release_service':types.SimpleNamespace(register_signed_client_release=registry,store_client_release_artifact=storage),
                }
                with patch.dict(sys.modules,modules), patch.object(sys,'argv',['register.py',tmp,TARGET_VERSION,'f'*64,operation]):
                    if expected_error:
                        with self.assertRaisesRegex(RuntimeError,expected_error):
                            runpy.run_path(str(ROOT/'ops/formal_release/register.py'))
                        registry.assert_not_called();storage.assert_not_called();transaction.commit.assert_not_called()
                    else:
                        runpy.run_path(str(ROOT/'ops/formal_release/register.py'))
                        transaction.commit.assert_called_once()
                        calls=[str(c) for c in db.execute.call_args_list]
                        self.assertTrue(any('LOCK TABLE workers, tasks IN SHARE MODE' in c for c in calls))
                        self.assertEqual(registry.call_count, int(operation=='publish'))
                        self.assertEqual(storage.call_count, int(operation=='publish'))


if __name__ == "__main__":
    unittest.main()
