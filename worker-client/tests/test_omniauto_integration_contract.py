from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests.contract_artifacts import resolve_contract_artifact


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "adapters"


class OmniAutoIntegrationContractTest(unittest.TestCase):
    def test_generated_observation_schema_is_current_and_validates_shared_fixture(self):
        check = subprocess.run(
            [sys.executable, "scripts/generate-c2-observation-schema.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

        sidecar_path = ADAPTERS / "wechat_win32_ocr_sidecar.py"
        module_name = "chejin_generated_contract_sidecar"
        spec = importlib.util.spec_from_file_location(module_name, sidecar_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        fixture_path = resolve_contract_artifact(
            "examples",
            "c2_v3_mixed_roundtrip.json",
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        for observation in fixture["omniauto_output"]["observations"]:
            self.assertEqual(
                module.validate_message_observation_v3(observation),
                [],
                observation["observation_id"],
            )

        generated = json.loads(
            (ADAPTERS / "chejin_c2_observation_schema.generated.json").read_text(encoding="utf-8")
        )
        self.assertEqual(module.C2_OBSERVATION_CONTRACT_REVISION, generated["contract_revision"])
        self.assertEqual(module.C2_OBSERVATION_CONTRACT_SHA256, generated["contract_sha256"])

    def test_runner_connector_methods_share_conversation_type_contract(self):
        connector_path = ADAPTERS / "wechat_connector.py"
        source = connector_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(connector_path))
        connector = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WeChatConnector"
        )
        methods = {
            node.name: node
            for node in connector.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("get_messages", "transcribe_voice_messages", "send_text", "send_text_and_verify"):
            method = methods[name]
            keyword_names = {argument.arg for argument in method.args.kwonlyargs}
            self.assertIn("conversation_type", keyword_names, f"{name} must accept conversation_type")

        runner = (ADAPTERS / "wechat_sidecar_runner.py").read_text(encoding="utf-8")
        adapter = (ADAPTERS / "wechat_pr28_runtime_adapter.py").read_text(encoding="utf-8")
        sidecar = (ADAPTERS / "wechat_win32_ocr_sidecar.py").read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--conversation-type"', runner)
        self.assertIn('"--conversation-type",', sidecar)
        self.assertIn("C2 private admission still requires title OCR evidence", sidecar)
        self.assertIn("def transcribe_voice_messages", adapter)
        self.assertGreaterEqual(source.count('args.extend(["--conversation-type", clean_conversation_type])'), 3)
        self.assertIn('send_kwargs["conversation_type"] = clean_conversation_type', source)

    def test_runtime_adapter_applies_same_identity_projection_to_voice(self):
        adapter_path = ADAPTERS / "wechat_pr28_runtime_adapter.py"
        module_name = "chejin_wechat_pr28_runtime_adapter"
        spec = importlib.util.spec_from_file_location(module_name, adapter_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        class FakeConnector:
            def __init__(self):
                self.calls = []

            def call_compat_sidecar(self, *_args, **_kwargs):
                return {"ok": True}

            def transcribe_voice_messages(self, target, exact=True, **kwargs):
                self.calls.append({"target": target, "exact": exact, **kwargs})
                return {"ok": True}

        raw = FakeConnector()
        adapter = module.adapt_wechat_pr28_connector(raw)
        adapter.transcribe_voice_messages(
            "张三-CJ123",
            session_key="opaque-row-key",
            conversation_type="private",
        )
        self.assertEqual(raw.calls[-1]["session_key"], "opaque-row-key")
        self.assertEqual(raw.calls[-1]["conversation_type"], "")

        adapter.transcribe_voice_messages("张三-CJ123", conversation_type="private")
        self.assertEqual(raw.calls[-1]["conversation_type"], "private")

    def test_integration_does_not_restore_retired_image_or_vision_routes(self):
        connector = (ADAPTERS / "wechat_connector.py").read_text(encoding="utf-8")
        adapter = (ADAPTERS / "wechat_pr28_runtime_adapter.py").read_text(encoding="utf-8")
        combined = connector + "\n" + adapter
        self.assertNotIn("target_not_confirmed_for_image_save", combined)
        self.assertNotIn("run_customer_clipboard_image_transaction", combined)
        self.assertNotIn("run_self_clipboard_image_transaction", combined)


if __name__ == "__main__":
    unittest.main()
