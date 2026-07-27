from __future__ import annotations

import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from chejin_worker_client.artifact_retention import (
    cleanup_artifacts,
    record_artifact_outcome,
)


class ArtifactRetentionTest(unittest.TestCase):
    def _flow(
        self,
        app_dir: Path,
        *,
        category: str,
        name: str,
        age_days: int,
        critical: bool,
        size: int = 64,
    ) -> Path:
        path = app_dir / "artifacts" / "wechat_c2" / category / name
        path.mkdir(parents=True)
        (path / "capture.png").write_bytes(b"x" * size)
        record_artifact_outcome(
            path,
            {"ok": not critical, "error_code": "FAILED" if critical else ""},
        )
        timestamp = (
            datetime.now(timezone.utc) - timedelta(days=age_days)
        ).timestamp()
        for item in path.iterdir():
            os.utime(item, (timestamp, timestamp))
        os.utime(path, (timestamp, timestamp))
        return path

    def test_retention_keeps_current_flow_and_uses_separate_success_failure_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp)
            expired_success = self._flow(
                app_dir,
                category="messages",
                name="20260701_100000_success",
                age_days=8,
                critical=False,
            )
            protected_success = self._flow(
                app_dir,
                category="messages",
                name="20260701_100001_active",
                age_days=8,
                critical=False,
            )
            retained_critical = self._flow(
                app_dir,
                category="voice",
                name="20260701_100002_failed",
                age_days=20,
                critical=True,
            )
            expired_critical = self._flow(
                app_dir,
                category="voice",
                name="20260701_100003_failed",
                age_days=31,
                critical=True,
            )

            result = cleanup_artifacts(
                app_dir=app_dir,
                protected_paths=[protected_success],
                max_bytes=1024 * 1024,
            )

            self.assertFalse(expired_success.exists())
            self.assertTrue(protected_success.exists())
            self.assertTrue(retained_critical.exists())
            self.assertFalse(expired_critical.exists())
            self.assertEqual(result.deleted_directories, 2)
            self.assertGreaterEqual(result.deleted_files, 4)
            self.assertGreater(result.released_bytes, 0)

    def test_capacity_evicts_success_before_critical(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp)
            success = self._flow(
                app_dir,
                category="messages",
                name="20260720_100000_success",
                age_days=1,
                critical=False,
                size=200,
            )
            critical = self._flow(
                app_dir,
                category="voice",
                name="20260720_100001_failed",
                age_days=1,
                critical=True,
                size=200,
            )

            cleanup_artifacts(app_dir=app_dir, max_bytes=500)

            self.assertFalse(success.exists())
            self.assertTrue(critical.exists())

    def test_cleanup_rejects_any_root_outside_worker_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp) / "worker"
            outside = Path(temp) / "outside"
            outside.mkdir(parents=True)

            with self.assertRaisesRegex(
                ValueError,
                "ARTIFACT_CLEANUP_ROOT_OUTSIDE_WORKER_HOME",
            ):
                cleanup_artifacts(app_dir=app_dir, artifacts_root=outside)

    def test_late_vision_failure_promotes_previously_successful_flow_to_critical(self):
        with tempfile.TemporaryDirectory() as temp:
            flow = Path(temp) / "artifacts" / "wechat_c2" / "messages" / "20260723_120000"
            flow.mkdir(parents=True)
            record_artifact_outcome(flow, {"ok": True})
            record_artifact_outcome(
                flow,
                {"ok": False, "error_code": "vision_provider_timeout"},
            )

            marker = json.loads(
                (flow / ".chejin-retention.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["retention_class"], "critical")
            self.assertEqual(marker["error_code"], "vision_provider_timeout")

    def test_marker_write_failure_is_best_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            flow = Path(temp) / "artifacts" / "tasks" / "task-1"
            flow.mkdir(parents=True)

            with patch.object(
                Path,
                "write_text",
                side_effect=OSError("disk full"),
            ):
                recorded = record_artifact_outcome(
                    flow,
                    {"ok": True, "result_code": "invite_sent"},
                )

            self.assertFalse(recorded)


if __name__ == "__main__":
    unittest.main()
