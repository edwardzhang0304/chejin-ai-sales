"""External send boundary for the Worker bridge regression, not a Windows test.

The successful envelope comes from the current production mock adapter instead
of duplicating its schema here. The parent executes the real subprocess, JSON,
evidence and result-classification path. No real WeChat operation is performed.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chejin_worker_client.rpa_bridge import RpaBridge

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["send"])
parser.add_argument("--target", required=True)
parser.add_argument("--text", required=True)
parser.add_argument("--session-key", default="")
parser.add_argument("--artifact-dir", type=Path, required=True)
parser.add_argument("--current-only", action="store_true")
parser.add_argument("--expected-context-guard")
parser.add_argument("--action-journal")
args = parser.parse_args()
assert args.current_only
bridge = RpaBridge()
bridge.mode = "mock"
payload = bridge.send_reply(
    target=args.target, rpa_session_key=args.session_key, text=args.text,
    task_id="boundary-fixture", current_only=args.current_only,
)
mode = os.environ.get("SEND_RESULT_TEST_MODE", "sent")
assert mode in {"sent", "failed", "unknown"}
if mode != "sent":
    # Fault injection at the external boundary; do not change Worker handling.
    phase = "not_attempted" if mode == "failed" else "trigger_attempted"
    payload.update(ok=False, action_phase=phase, error_code="TEST_SEND_" + mode.upper())
    payload["physical_send_triggered"] = mode == "unknown"
    payload["send_result"].update(
        ok=False, confirmed=False, result=mode, action_phase=phase,
        physical_send_triggered=mode == "unknown",
    )
if args.action_journal:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "omniauto-rpa"))
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import write_action_phase_journal

    write_action_phase_journal(args.action_journal, payload["action_phase"])
with (args.artifact_dir / "boundary-calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"action": args.action, "pid": os.getpid()}) + "\n")
(args.artifact_dir / "boundary-result.json").write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
raise SystemExit(0 if payload["ok"] else 1)
