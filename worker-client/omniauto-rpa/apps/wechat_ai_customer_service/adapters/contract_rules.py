"""Pure contract interpretation shared by backend and Worker; no I/O or state."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any


IMAGE_FORBIDDEN_FIELD_PREFIXES = (
    "provider_response", "raw_provider_response", "retry_response", "initial_response",
)


def pre_send_reidentification_errors(payload: dict[str, Any]) -> frozenset[str]:
    section = payload.get("pre_send_message_viewport_contract")
    if not isinstance(section, dict):
        raise RuntimeError("Invalid C2 pre_send_message_viewport_contract")
    return contract_values(section, "pre_send_specific_errors") - {
        "C2_PRE_SEND_LAYOUT_INVALID", "C2_PRE_SEND_FACT_CHECKPOINT_INVALID",
    }


def contract_values(payload: dict[str, Any], key: str) -> frozenset[str]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"Invalid C2 contract list: {key}")
    return frozenset(str(item) for item in values)


def contract_value_map(payload: dict[str, Any], key: str) -> dict[str, frozenset[str]]:
    values = payload.get(key)
    if not isinstance(values, dict):
        raise RuntimeError(f"Invalid C2 contract map: {key}")
    result: dict[str, frozenset[str]] = {}
    for map_key, items in values.items():
        if not isinstance(items, list):
            raise RuntimeError(f"Invalid C2 contract map values: {key}.{map_key}")
        result[str(map_key)] = frozenset(str(item) for item in items)
    return result


def contract_revision(payload: dict[str, Any]) -> str:
    value = str(payload.get("contract_revision") or "").strip()
    if not value:
        raise RuntimeError("Invalid C2 contract revision")
    return value


def contract_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def contract_rules_sha256(payload: dict[str, Any]) -> str:
    """Fingerprint every rule; a release label is not a business rule."""
    return contract_sha256({key: value for key, value in payload.items() if key != "contract_revision"})


def equivalent_contract(
    current: dict[str, Any], revision: Any, sha256: Any,
) -> dict[str, Any] | None:
    """Verify the sender's complete contract without rewriting its evidence.

    Only the release label may differ. A changed rule, missing hash or an
    invented revision paired with a different contract's hash is rejected.
    The returned validator input preserves the sender's original revision.
    """
    if (not isinstance(revision, str) or len(revision) > 64
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", revision)):
        return None
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        return None
    candidate = {**current, "contract_revision": revision}
    return candidate if hmac.compare_digest(contract_sha256(candidate), sha256.lower()) else None


RELEASED_READ_RULES_SHA256 = 'bd5f2a2fadcae7575651617ce642f35594ae5da931eede673efe979308e6079f'

# Reviewed sequence v1 -> published 0.9.85 -> pre-terminal read rules.
# These fingerprints exclude only the release label. Changing any other rule
# requires another reviewed migration, not an update to the old fingerprint.
_SEQUENCE_V1_RULES_SHA256 = '26e8dabaa29677d6f4e5d845688c34916c5723b63ad93dae944031bc1b0b1320'
_HISTORICAL_CORRECTION_V1_RULES_SHA256 = '6d9dac87d16e91211145927d38b1ccbc1ee32c835d64dd1fca3dbcf4d5315891'
_TEXT_CORRESPONDENCE_V1_RULES_SHA256 = 'c5154ddaad1494673d0507800762871821eeb7af6da7327ce3bf0a778156bece'
_CORRECTION_PENDING_V1_RULES_SHA256 = '8cb4d6d2e3cc5df3760f36529216eb6419e4074264a403adb0e7393090bf05ae'
_CORRECTION_RECHECK_V1_RULES_SHA256 = 'ea8b203ac06247f0635abba2fc5971652bea5e95436369066bb0958983d6fb36'
_CORRECTION_RESOLUTION_V1_RULES_SHA256 = '8623691392df801c7d6fdf8c12268d9fa75f8b213cea3f967c47a2e042e63ef8'
_PRE_SEND_READ_V1_RULES_SHA256 = 'fbf33b38f2d9493f02534d71720be355c7cdfe69ad903b7ceb39dc10c2396a7a'
_PUBLISHED_085_RULES_SHA256 = '6ee655ac27557a0c24070b1218b34eab094f2e8d89011a8ea115e155b2aa6b75'
RELEASED_READ_CONTRACTS = (
    ('0.9.89', 'aed5f736db2af32854d2bb5449ccc79479897eaf6bb5224d1c2bbb2d8dd0d311'),
    ('0.9.75', 'bcb1af09321339b159cc02581f5938e402f16094465933645c71bd7dc0eadcf1'),
    ('0.9.78', 'b4151ab61fb5d90688e1e0ac187cc767acaee1617cb420be3028acc52ccf7eab'),
    ('0.9.80', '43f8c07e3660d790c39f3b348dcce9fb1e2c0bed243b41cff6669a658995e380'),
    # Actual published 80988ea contract, not the same-labelled sequence dev tree.
    ('0.9.85', '891f245e353c78e0a9f0b24e607be8bdbb2b59fc993e9df2935f0f416ff8f186'),
    ('0.9.87', 'bdd71a5bb3b6b517db1cf693066eb9652b962e58d33de855a862a0961cbf2ece'),
    # Published ccbc56b: sequence v1, before pre-send read recovery was added.
    ('0.9.86', 'fa530187463e11e8cdb340417139bd44a1aa27af8137e97efd3ffdc199a71f96'),
)


def _pre_send_read_predecessor(current: dict) -> dict | None:
    # Only reconstruct the original read validator. Do not admit the new
    # pre-send proof/receipt rules to an old Flow or change stored evidence.
    if contract_rules_sha256(current) != _PRE_SEND_READ_V1_RULES_SHA256:
        return None
    previous = {key: value for key, value in current.items() if key != 'pre_send_read_recovery_contract'}
    return previous if contract_rules_sha256(previous) == _SEQUENCE_V1_RULES_SHA256 else None


def _sequence_read_predecessor(current: dict) -> dict | None:
    if contract_rules_sha256(current) != _SEQUENCE_V1_RULES_SHA256:
        return None
    previous = {key: value for key, value in current.items() if key != 'c3_reply_sequence_contract'}
    previous['pre_send_fact_checkpoint_contract'] = {
        **current['pre_send_fact_checkpoint_contract'],
        'storage': 'MessageBatch.ai_request_snapshot.pre_send_fact_checkpoint',
    }
    return previous if contract_rules_sha256(previous) == _PUBLISHED_085_RULES_SHA256 else None


def read_recovery_contract(current: dict, revision: Any, sha256: Any) -> dict | None:
    """Reviewed read-settlement migrations, restricted by their caller.

    All previously published read rules must match the frozen fingerprint.
    This does not admit old clients to new work or claim full-rule equivalence.
    Any later change to message rules requires another reviewed migration.
    """
    same = equivalent_contract(current, revision, sha256)
    if same is not None:
        return same
    if 'historical_text_correction_recheck_contract' in current:
        if contract_rules_sha256(current) != _CORRECTION_RECHECK_V1_RULES_SHA256:
            return None
        previous = {key: value for key, value in current.items() if key != 'historical_text_correction_recheck_contract'}
        if contract_rules_sha256(previous) != _CORRECTION_RESOLUTION_V1_RULES_SHA256:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'historical_text_correction_resolution_contract' in current:
        if contract_rules_sha256(current) != _CORRECTION_RESOLUTION_V1_RULES_SHA256:
            return None
        previous = {key: value for key, value in current.items() if key != 'historical_text_correction_resolution_contract'}
        if contract_rules_sha256(previous) != _CORRECTION_PENDING_V1_RULES_SHA256:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'historical_text_correction_pending_contract' in current:
        if contract_rules_sha256(current) != _CORRECTION_PENDING_V1_RULES_SHA256:
            return None
        previous = {key: value for key, value in current.items() if key != 'historical_text_correction_pending_contract'}
        if contract_rules_sha256(previous) != _TEXT_CORRESPONDENCE_V1_RULES_SHA256:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'text_correspondence_contract' in current:
        if contract_rules_sha256(current) != _TEXT_CORRESPONDENCE_V1_RULES_SHA256:
            return None
        previous = {key: value for key, value in current.items() if key != 'text_correspondence_contract'}
        if contract_rules_sha256(previous) != _HISTORICAL_CORRECTION_V1_RULES_SHA256:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'historical_text_correction_contract' in current:
        if contract_rules_sha256(current) != _HISTORICAL_CORRECTION_V1_RULES_SHA256:
            return None
        previous = {key: value for key, value in current.items()
                    if key != 'historical_text_correction_contract'}
        if contract_rules_sha256(previous) != _PRE_SEND_READ_V1_RULES_SHA256:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'pre_send_read_recovery_contract' in current:
        previous = _pre_send_read_predecessor(current)
        if previous is None:
            return None
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    if 'c3_reply_sequence_contract' in current:
        previous = _sequence_read_predecessor(current)
        if previous is None:
            return None
        # Only the validator input is reconstructed. Stored evidence retains
        # its original bytes/version/SHA; this never authorizes new work.
        current = previous
        same = equivalent_contract(current, revision, sha256)
        if same is not None:
            return same
    protocol = current.get('terminal_read_settlement_contract') or {}
    original = {key: value for key, value in current.items() if key != 'terminal_read_settlement_contract'}
    if protocol.get('protocol_version') != 1 or contract_rules_sha256(original) != RELEASED_READ_RULES_SHA256:
        return None
    return equivalent_contract(original, revision, sha256)


def recovery_contract_capability(current: dict, frozen_contracts: dict[str, dict]) -> dict:
    """Build identical backend/package declarations from verified resources.

    Hosts own resource I/O and original-Flow/fact-only authorization. This pure
    function checks every declared historical resource and reviewed migration.
    """
    pairs = []
    for revision, sha256 in RELEASED_READ_CONTRACTS:
        historical = frozen_contracts.get(revision)
        if historical is None:
            raise RuntimeError('RECOVERY_CONTRACT_MISSING')
        if contract_sha256(historical) != sha256:
            raise RuntimeError('RECOVERY_CONTRACT_CORRUPTED')
        if read_recovery_contract(current, revision, sha256) != historical:
            raise RuntimeError('RECOVERY_CONTRACT_SEMANTICS_CHANGED')
        pairs.append({'revision': revision, 'sha256': sha256})
    pairs.append({'revision': contract_revision(current), 'sha256': contract_sha256(current)})
    return {'protocol_version': 1, 'compatible_rules_sha256': contract_rules_sha256(current),
            'contracts': pairs}


def contract_row_rules(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = payload.get("row_rules")
    if not isinstance(values, dict):
        raise RuntimeError("Invalid C2 contract row_rules")
    rules: dict[str, dict[str, Any]] = {}
    for row_kind, raw_rule in values.items():
        if not isinstance(raw_rule, dict):
            raise RuntimeError(f"Invalid C2 row rule: {row_kind}")
        rules[str(row_kind)] = dict(raw_rule)
    if set(rules) != set(contract_values(payload, "row_kinds")):
        raise RuntimeError("C2 row_rules and row_kinds are inconsistent")
    declared_ingestible = set(contract_values(payload, "ingestible_row_kinds"))
    derived_ingestible = {row_kind for row_kind, rule in rules.items() if bool(rule.get("ingestible"))}
    if declared_ingestible != derived_ingestible:
        raise RuntimeError("C2 ingestible_row_kinds and row_rules are inconsistent")
    return rules


def image_contract(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("image_contract")
    if not isinstance(value, dict):
        raise RuntimeError("Invalid C2 contract image_contract")
    return dict(value)


def validate_image_result_schema(
    payload: dict[str, Any],
    value: Any,
    schema_name: str,
) -> list[str]:
    schemas = image_contract(payload).get("schemas")
    schema = schemas.get(schema_name) if isinstance(schemas, dict) else None
    if not isinstance(schema, dict):
        raise RuntimeError(f"Invalid C2 image schema: {schema_name}")
    errors: list[str] = []

    def reject_non_finite(item: Any, path: str) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            errors.append(f"{path}: non-finite number")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                reject_non_finite(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                reject_non_finite(child, f"{path}[{index}]")

    reject_non_finite(value, "$")
    try:
        from jsonschema import Draft7Validator
    except ImportError as exc:
        raise RuntimeError("jsonschema dependency is required") from exc
    validator = Draft7Validator(schema)
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        errors.append(f"{path}: {error.message}")
    return errors


def recovery_action_for_error(
    payload: dict[str, Any], error_code: str, status_code: int,
    recovery_action: str | None = "",
) -> str:
    """Return the single contract-owned recovery action for an API error."""

    contract = payload.get("outbox_recovery_contract")
    if not isinstance(contract, dict):
        raise RuntimeError("Invalid C2 outbox_recovery_contract")
    explicit = str(recovery_action or "").strip()
    if explicit in {str(value) for value in (contract.get("actions") or [])}:
        return explicit
    code = str(error_code or "").strip()
    correction = payload.get('historical_text_correction_contract') or {}
    if (code == 'HISTORICAL_TEXT_CORRECTION_BUSY' and int(status_code) == 409
            and correction.get('protocol_version') == 1
            and correction.get('old_sending_or_unknown') == 'busy_defer'):
        return 'retry'
    correspondence = payload.get("text_correspondence_contract") or {}
    if (code == "TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED"
            and correspondence.get("version") == 1
            and correspondence.get("expired_recovery_action") == "refresh_and_rebuild"):
        return "refresh_and_rebuild"
    code_groups = (
        ("identity_quarantined_codes", "identity_quarantined"),
        ("refresh_and_rebuild_codes", "refresh_and_rebuild"),
        ("split_and_retry_codes", "split_and_retry"),
        ("target_terminated_codes", "target_terminated"),
        ("conversation_terminated_codes", "conversation_terminated"),
        ("capability_paused_codes", "capability_paused"),
    )
    for field, action in code_groups:
        if code in {
            str(value)
            for value in (contract.get(field) or [])
        }:
            return action
    if int(status_code) in {
        int(value) for value in (contract.get("retry_statuses") or [])
    } or int(status_code) >= 500:
        return "retry"
    return str(
        contract.get("unknown_api_error_action")
        or "capability_paused"
    )
