from __future__ import annotations

import ast
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from chejin_worker_client.message_identity_commit import (
    CommittedMessage,
    IdentityCommitRejection,
    MediaActionTerminal,
    MessageCommitBasis,
    RuntimeIdentityObject,
    committed_identity_record,
    commit_message_identity,
)
from chejin_worker_client.wechat_c2 import (
    WechatReadTarget,
    build_message_ingest_payload,
    image_observation_source_key,
)


CONVERSATION_ID = "conversation-identity-lifecycle"


def _parametrize(argnames, argvalues):
    """Keep table-driven tests runnable by both unittest and pytest."""

    names = (
        tuple(argnames)
        if isinstance(argnames, (tuple, list))
        else tuple(part.strip() for part in str(argnames).split(","))
    )

    def decorate(function):
        def wrapped():
            for raw_values in argvalues:
                values = raw_values if len(names) > 1 else (raw_values,)
                function(*values)

        wrapped.__name__ = function.__name__
        wrapped.__doc__ = function.__doc__
        return wrapped

    return decorate


@contextmanager
def _raises(exception_type, *, match: str = ""):
    try:
        yield
    except exception_type as exc:
        if match and re.search(match, str(exc)) is None:
            raise AssertionError(
                f"exception {exc!r} does not match {match!r}"
            ) from exc
    else:
        raise AssertionError(f"{exception_type.__name__} was not raised")


def load_tests(_loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite


def _observation(
    *,
    message_type: str,
    basis: MessageCommitBasis,
    stable_id: str = "worker-message-7",
    observation_id: str = "observation-7",
) -> dict:
    role = "self" if basis is MessageCommitBasis.CONFIRMED_SENT_ACK else "customer"
    row_kind = {
        "text": "text_bubble",
        "system": "system_row",
        "voice": "voice_bubble",
        "image": "image_bubble",
    }[message_type]
    proof: dict = {}
    item = {
        "observation_id": observation_id,
        "row_kind": row_kind,
        "sender_role": role,
        "message_type": message_type,
        "_worker_stable_id": stable_id,
        "_worker_identity_scope": "committed",
    }
    if basis is MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT:
        proof = {
            "alignment_status": "unique",
            "worker_stable_id": stable_id,
            "pre_observation_id": "checkpoint-source-7",
            "post_observation_id": observation_id,
            "match_basis": "native_source_message_id",
        }
    elif basis is MessageCommitBasis.NEW_SUFFIX:
        proof = {
            "alignment_status": "not_required",
            "old_tail_fully_consumed": True,
            "new_suffix_observation_id": observation_id,
        }
    elif basis in {
        MessageCommitBasis.CONFIRMED_VOICE_ACTION,
        MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
    }:
        mapping = {
            "canonical_action_id": f"{message_type}-action-7",
            "reserved_worker_stable_id": stable_id,
            "pre_observation_id": "pre-observation-7",
            "post_observation_id": observation_id,
            "binding_confirmed": True,
            "trigger_observation_id": observation_id,
            "physical_identity_inherited_from_prepare": False,
        }
        if message_type == "voice":
            mapping.update(
                {
                    "selected_action_token": "voice-token-7",
                    "physical_action_count": 1,
                    "result_candidate_count": 1,
                    "stable_business_content_signature": hashlib.sha256(
                        b"voice-transcript-7"
                    ).hexdigest(),
                }
            )
        proof = dict(mapping)
        if message_type == "voice":
            item["_worker_voice_action_summary"] = {
                "confirmed_action_mapping": mapping,
            }
        else:
            fingerprint = "dhash64:0123456789abcdef"
            image_sha256 = hashlib.sha256(b"image-bytes-7").hexdigest()
            proof["image_sha256"] = image_sha256
            item["image_physical_anchor"] = {
                "bubble_visual_fingerprint": fingerprint,
            }
            item["_worker_image_action_summary"] = {
                "confirmed_action_mapping": mapping,
                "image_visual_fingerprint": fingerprint,
                "image_sha256": image_sha256,
            }
    elif basis is MessageCommitBasis.CONFIRMED_SENT_ACK:
        proof = {"reply_action_id": "reply-action-7"}
        item["_worker_ai_reply_receipt"] = {
            "reply_action_id": "reply-action-7",
            "reply_text_hash": "sha256:reply-7",
            "worker_stable_id": stable_id,
            "confirmed_at": "2026-08-16T00:00:00+00:00",
        }
    elif basis is MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID:
        item["native_source_message_id"] = "wx-native-message-7"
        proof = {
            "native_source_message_id": "wx-native-message-7",
            "sender_role": role,
            "message_type": message_type,
        }
    item["_worker_committed_message"] = committed_identity_record(
        worker_stable_id=stable_id,
        commit_basis=basis,
        observation_id=observation_id,
        sender_role=role,
        message_type=message_type,
        proof=proof,
    )
    return item


@_parametrize(
    ("message_type", "basis"),
    [
        ("text", MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT),
        ("text", MessageCommitBasis.NEW_SUFFIX),
        ("voice", MessageCommitBasis.CONFIRMED_VOICE_ACTION),
        ("image", MessageCommitBasis.CONFIRMED_IMAGE_ACTION),
        ("text", MessageCommitBasis.CONFIRMED_SENT_ACK),
        ("text", MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID),
        ("voice", MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID),
        ("image", MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID),
    ],
)
def test_every_allowed_commit_basis_returns_one_typed_message(message_type, basis):
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=_observation(message_type=message_type, basis=basis),
    )
    assert isinstance(result, CommittedMessage)
    assert result.commit_basis is basis
    assert result.message_type == message_type
    assert result.source_message_key.startswith("source:")


def test_commit_gate_preserves_existing_worker_sequence_source_key_bytes():
    item = _observation(
        message_type="text",
        basis=MessageCommitBasis.NEW_SUFFIX,
    )
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=item,
    )
    assert isinstance(result, CommittedMessage)
    legacy_raw = json.dumps(
        {
            "conversation_id": CONVERSATION_ID,
            "identity_kind": "worker_sequence",
            "identity": "worker-message-7",
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    assert result.source_message_key == (
        "source:"
        + hashlib.sha1(legacy_raw.encode("utf-8")).hexdigest()[:40]
    )


@_parametrize(
    "runtime_object",
    [
        RuntimeIdentityObject.FRAME_OBSERVATION,
        RuntimeIdentityObject.PENDING_MEDIA_ACTION,
        RuntimeIdentityObject.QUARANTINE_RECORD,
        "",
        "unknown",
    ],
)
def test_noncommitted_runtime_objects_hit_real_source_and_v3_gates(
    runtime_object,
):
    item = _observation(
        message_type="image",
        basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
    )
    item["_worker_committed_message"]["object_type"] = str(
        getattr(runtime_object, "value", runtime_object)
    )
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=item,
    )
    assert isinstance(result, IdentityCommitRejection)
    assert not hasattr(result, "source_message_key")
    target = WechatReadTarget(
        conversation_id=CONVERSATION_ID,
        rpa_session_key="wx:identity-consumer-matrix",
        display_name="CJIDM001",
        remark_code="CJIDM001",
        authorization_revision="revision-identity-consumer-matrix",
    )
    with _raises(ValueError, match="C2_IMAGE_IDENTITY_CONTRACT_INVALID"):
        image_observation_source_key(target, item)
    with _raises(ValueError, match="C2_IMAGE_IDENTITY_CONTRACT_INVALID"):
        build_message_ingest_payload(
            target,
            {
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": [item],
            },
            read_run_id="read-identity-consumer-matrix",
        )


@_parametrize(
    ("field", "value"),
    [
        ("_worker_committed_message", None),
        ("_worker_identity_scope", ""),
        ("_worker_identity_scope", "unknown"),
        ("_worker_identity_scope", "current_read_provisional"),
        ("_worker_stable_id", ""),
        ("_worker_stable_id", "worker-message-99"),
    ],
)
def test_missing_blank_unknown_and_contradictory_identity_fail_closed(field, value):
    item = _observation(
        message_type="text",
        basis=MessageCommitBasis.NEW_SUFFIX,
    )
    if value is None:
        item.pop(field, None)
    else:
        item[field] = value
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=item,
    )
    assert isinstance(result, IdentityCommitRejection)


@_parametrize("message_type", ["voice", "image"])
def test_media_with_reserved_id_but_no_confirmed_receipt_is_not_committed(message_type):
    basis = (
        MessageCommitBasis.CONFIRMED_VOICE_ACTION
        if message_type == "voice"
        else MessageCommitBasis.CONFIRMED_IMAGE_ACTION
    )
    item = _observation(message_type=message_type, basis=basis)
    item.pop(
        "_worker_voice_action_summary"
        if message_type == "voice"
        else "_worker_image_action_summary"
    )
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=item,
    )
    assert isinstance(result, IdentityCommitRejection)


def test_media_terminal_states_are_mece():
    assert {item.value for item in MediaActionTerminal} == {
        "cancelled_before_trigger",
        "committed_completed",
        "committed_failed",
        "identity_unresolved",
        "technical_failed",
    }


def test_production_source_key_callers_use_the_commit_gate():
    root = Path(__file__).resolve().parents[1] / "chejin_worker_client"
    production = [
        root / "wechat_c2.py",
        root / "task_runner.py",
        root / "c2_outbox_recovery.py",
    ]
    generic_calls = []
    for path in production:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "worker_source_message_key":
                generic_calls.append((path.name, node.lineno))
    assert generic_calls == []


def test_each_image_receipt_field_is_mandatory():
    valid = _observation(
        message_type="image",
        basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
    )
    for field in (
        "canonical_action_id",
        "reserved_worker_stable_id",
        "pre_observation_id",
        "post_observation_id",
        "binding_confirmed",
    ):
        item = copy.deepcopy(valid)
        item["_worker_image_action_summary"]["confirmed_action_mapping"][field] = (
            False if field == "binding_confirmed" else ""
        )
        result = commit_message_identity(
            conversation_id=CONVERSATION_ID,
            observation=item,
        )
        assert isinstance(result, IdentityCommitRejection), field


def test_each_voice_receipt_field_is_mandatory():
    valid = _observation(
        message_type="voice",
        basis=MessageCommitBasis.CONFIRMED_VOICE_ACTION,
    )
    for field in (
        "canonical_action_id",
        "reserved_worker_stable_id",
        "pre_observation_id",
        "post_observation_id",
        "binding_confirmed",
    ):
        item = copy.deepcopy(valid)
        item["_worker_voice_action_summary"]["confirmed_action_mapping"][field] = (
            False if field == "binding_confirmed" else ""
        )
        result = commit_message_identity(
            conversation_id=CONVERSATION_ID,
            observation=item,
        )
        assert isinstance(result, IdentityCommitRejection), field


@_parametrize("message_type", ["voice", "image"])
def test_historical_media_rejects_single_sided_or_order_only_proof(message_type):
    item = _observation(
        message_type=message_type,
        basis=MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT,
    )
    for match_basis in ("ordered_compatible", "single_sided_context", ""):
        candidate = copy.deepcopy(item)
        candidate["_worker_committed_message"]["proof"]["match_basis"] = (
            match_basis
        )
        result = commit_message_identity(
            conversation_id=CONVERSATION_ID,
            observation=candidate,
        )
        assert isinstance(result, IdentityCommitRejection), match_basis


@_parametrize("message_type", ["voice", "image"])
def test_action_summary_and_commit_record_must_describe_the_same_receipt(message_type):
    basis = (
        MessageCommitBasis.CONFIRMED_VOICE_ACTION
        if message_type == "voice"
        else MessageCommitBasis.CONFIRMED_IMAGE_ACTION
    )
    item = _observation(message_type=message_type, basis=basis)
    item["_worker_committed_message"]["proof"]["canonical_action_id"] = (
        "another-action"
    )
    result = commit_message_identity(
        conversation_id=CONVERSATION_ID,
        observation=item,
    )
    assert isinstance(result, IdentityCommitRejection)


def test_durable_production_consumers_have_no_legacy_identity_bypass():
    root = Path(__file__).resolve().parents[1] / "chejin_worker_client"
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / "wechat_c2.py",
            root / "task_runner.py",
            root / "c2_outbox_recovery.py",
            root / "storage.py",
        )
    )
    for forbidden in (
        "def worker_source_message_key(",
        "rebuild_identity_collision",
        "refresh_identity_and_retry",
        "identity_replacement",
        "rebuild_invalid_media_as_failed",
        "rebuild_c2_outbox_payload",
        "rebuild_failed_facts",
    ):
        assert forbidden not in production_text


def test_source_keys_and_v3_builder_call_the_unique_commit_gate():
    root = Path(__file__).resolve().parents[1] / "chejin_worker_client"
    module = ast.parse((root / "wechat_c2.py").read_text(encoding="utf-8"))
    required_functions = {
        "image_observation_source_key",
        "voice_observation_source_key",
        "_build_message_ingest_payload_v3",
    }
    gate_calls: dict[str, set[str]] = {}
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in required_functions:
            continue
        called = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                called.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                called.add(child.func.attr)
        gate_calls[node.name] = called
    assert set(gate_calls) == required_functions
    assert "require_committed_message" in gate_calls["image_observation_source_key"]
    assert "require_committed_message" in gate_calls["voice_observation_source_key"]
    assert "require_committed_message" in gate_calls["_build_message_ingest_payload_v3"]
