import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reviewed_handoff as handoff


class ReviewedHandoffTests(unittest.TestCase):
    def test_contract_version_only_is_ignored_but_business_field_is_protected(self):
        old = b'{"contract_revision":"0.9.75","required":true}'
        self.assertEqual(handoff.normalized(handoff.CONTRACT, old), handoff.normalized(handoff.CONTRACT, old.replace(b"0.9.75", b"0.9.76")))
        self.assertNotEqual(handoff.normalized(handoff.CONTRACT, old), handoff.normalized(handoff.CONTRACT, old.replace(b"true", b"false")))

    def test_dependencies_tests_and_application_changes_are_not_normalized(self):
        for name in ("worker-client/requirements.txt", "backend/app/main.py", "worker-client/tests/test_storage.py"):
            self.assertNotEqual(handoff.normalized(name, b"old"), handoff.normalized(name, b"new"))

    def test_provenance_rejects_rewriting_old_integrations(self):
        old = {"current_release": {}, "selective_integrations": [{"source_commit": "a" * 40}], "runtime": "kept"}
        new = {**old, "current_release": {"version": "0.9.76", "contract_revision": "0.9.76", "contract_sha256": "b" * 64, "source_commit": "c" * 40},
               "selective_integrations": [*old["selective_integrations"], {"source_commit": "c" * 40, "scope": ["c2_contract_0_9_76_generated_schema"]}]}
        handoff.validate_provenance(json.dumps(old), json.dumps(new), "0.9.76", "b" * 64)
        for broken in ({**new, "runtime": "changed"}, {**new, "selective_integrations": new["selective_integrations"][1:]}):
            with self.assertRaises(ValueError): handoff.validate_provenance(json.dumps(old), json.dumps(broken), "0.9.76", "b" * 64)
