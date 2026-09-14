"""Portable rule compatibility: no host, backend, network or UI imports."""
import copy
import hashlib
import json
import unittest

from apps.wechat_ai_customer_service.adapters.contract_rules import (
    contract_rules_sha256, equivalent_contract,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class RuleCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.current = {'contract_revision': '1.2.4', 'messages': {
            'types': ['text', 'image'], 'required': ['sender'], 'max_bytes': 4096}}
        self.old = {**self.current, 'contract_revision': '1.2.3'}

    def test_version_only_change_preserves_original_validator_and_inputs(self):
        before = copy.deepcopy(self.current)
        accepted = equivalent_contract(self.current, '1.2.3', digest(self.old))
        self.assertEqual(accepted, self.old)
        self.assertEqual(self.current, before)
        self.assertEqual(contract_rules_sha256(self.current), contract_rules_sha256(self.old))

    def test_changed_added_or_removed_rules_never_inherit(self):
        altered = copy.deepcopy(self.old)
        altered['messages']['required'] = []
        added = {**self.old, 'new_rule': True}
        removed = {'contract_revision': '1.2.3'}
        for contract in (altered, added, removed):
            with self.subTest(contract=contract):
                self.assertIsNone(equivalent_contract(self.current, '1.2.3', digest(contract)))
                self.assertNotEqual(contract_rules_sha256(self.current), contract_rules_sha256(contract))

    def test_missing_malformed_or_mislabelled_identity_is_rejected(self):
        for revision, sha in ((None, digest(self.old)), ('01.2.3', digest(self.old)),
                              ('1.2.5', digest(self.old)), ('1.2.3', None),
                              ('1.2.3', 'x' * 64), ('1.2.3', digest(self.current))):
            with self.subTest(revision=revision, sha=sha):
                self.assertIsNone(equivalent_contract(self.current, revision, sha))


if __name__ == '__main__':
    unittest.main()
