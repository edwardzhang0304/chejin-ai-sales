"""Generate a low-risk RPA acceptance report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.rpa_acceptance_report import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    collect_rpa_acceptance_report,
    render_rpa_acceptance_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="chejin", help="Tenant id to inspect.")
    parser.add_argument(
        "--wechat-probe",
        choices=["none", "passive", "interactive"],
        default="none",
        help="Optional WeChat probe. Use passive for no-send screenshot/OCR validation.",
    )
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT), help="Runtime root to inspect.")
    parser.add_argument("--output-dir", default="", help="Directory for JSON and Markdown reports.")
    parser.add_argument("--allow-clipboard-once", action="store_true", help="Allow clipboard_once only for explicit burst-test acceptance.")
    args = parser.parse_args()

    report = collect_rpa_acceptance_report(
        tenant_id=args.tenant_id,
        runtime_root=Path(args.runtime_root),
        wechat_probe=args.wechat_probe,
        allow_clipboard_once=bool(args.allow_clipboard_once),
    )
    text = json.dumps(report, ensure_ascii=True, indent=2)
    print(text)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = DEFAULT_RUNTIME_ROOT / "test_artifacts" / "rpa_acceptance_report" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(text + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(render_rpa_acceptance_markdown(report), encoding="utf-8")
    print(f"Wrote {output_dir}")
    return 0 if report.get("status") != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
