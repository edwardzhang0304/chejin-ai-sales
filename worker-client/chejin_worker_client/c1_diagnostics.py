"""Task-scoped diagnostic sink. Never writes invitation/recovery authority."""
from datetime import datetime, timezone
import json
from pathlib import Path

from .failure_evidence import exception_details, record_capture_failure


class C1Diagnostics:
    def __init__(self, folder: Path, task_id: str):
        self.folder, self.task_id = Path(folder), task_id
        self.initialization_report = None

    def details(self, exc, ui=None):
        return {**exception_details(exc),
                'phase': getattr(ui, 'guard_phase', 'initialization'),
                'operation': getattr(ui, 'guard_operation', 'create_ui')}

    def capture_failure(self, target, exc, report):
        evidence = report.setdefault('evidence', {'frames': [], 'failures': []})
        item = {'target': target, **exception_details(exc)}
        if len(evidence['failures']) < 64:
            evidence['failures'].append(item)
        try:
            record_capture_failure('hook_c1', exc, error_code=report.get('error_code', ''),
                safe_context={'task_id': self.task_id, 'flow_id': self.task_id, 'target': target,
                              'original_failure': report.get('exception'),
                              'guard_failures': report.get('guard_failures', [])[:8]})
        except Exception as recorder_error:
            # Keep the failure in the formal outcome even if the independent
            # recorder fails. This metadata is not an invitation receipt.
            item['fallback_error'] = exception_details(recorder_error)

    def write(self, name, report):
        report.update(task_id=self.task_id, flow_id=self.task_id,
                      captured_at=datetime.now(timezone.utc).isoformat())
        try:
            path = self.folder/'diagnostics'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        except Exception as exc:
            self.capture_failure('diagnostics/'+name, exc, report)

    def initialization_failure(self, exc, ui=None):
        # Independent diagnostic only: ui-started remains authoritative and
        # no transaction outcome proving "UI untouched" is manufactured.
        report = {'stage': 'initialization', 'exception': self.details(exc, ui),
                  'guard_failures': getattr(ui, 'guard_failures', [])[:8],
                  'evidence': getattr(ui, 'evidence_state', {'frames': [], 'failures': []})}
        self.initialization_report = report
        self.write('initialization.json', report)
