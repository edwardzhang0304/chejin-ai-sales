from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
DEFAULT_OUTPUT = (
    ROOT
    / "omniauto-rpa"
    / "apps"
    / "wechat_ai_customer_service"
    / "adapters"
    / "chejin_c2_observation_schema.generated.json"
)


def resolve_contract_path() -> Path:
    candidates = (
        PROJECT_ROOT / "contracts" / "c2_contract_v3.json",
        ROOT / "contracts" / "c2_contract_v3.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("C2_CONTRACT_NOT_FOUND")


def canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generated_payload(contract: dict) -> dict:
    return {
        "generated_file": True,
        "source": "contracts/c2_contract_v3.json",
        "contract_revision": str(contract["contract_revision"]),
        "contract_sha256": canonical_sha256(contract),
        "observation_schema_version": int(contract["observation_schema_version"]),
        "action_phases": list(contract["action_phases"]),
        "sender_roles": list(contract["sender_roles"]),
        "row_rules": dict(contract["row_rules"]),
        "image_contract": dict(contract["image_contract"]),
        "message_limits": dict(contract["message_limits"]),
        "voice_action_binding_contract": dict(
            contract["voice_action_binding_contract"]
        ),
        "frame_action_binding_contract": dict(
            contract["frame_action_binding_contract"]
        ),
        "pre_send_message_viewport_contract": dict(
            contract["pre_send_message_viewport_contract"]
        ),
        "target_location_recovery_contract": dict(
            contract["target_location_recovery_contract"]
        ),
        "startup_layout_calibration_contract": dict(
            contract["startup_layout_calibration_contract"]
        ),
    }


def rendered_payload() -> str:
    contract = json.loads(resolve_contract_path().read_text(encoding="utf-8"))
    return json.dumps(generated_payload(contract), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered_payload()
    if args.check:
        actual = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if actual != expected:
            print(f"stale generated C2 observation schema: {args.output}")
            return 1
        print(f"C2 observation schema is current: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
