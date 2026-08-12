from __future__ import annotations

import unittest

from chejin_worker_client.startup_window_position import (
    WECHAT_WINDOW_GAP,
    position_to_right_of_wechat,
)


class StartupWindowPositionTest(unittest.TestCase):
    def test_uses_existing_wechat_status_geometry_with_small_gap(self):
        result = position_to_right_of_wechat(
            {
                "ok": True,
                "geometry": {
                    "left": 100,
                    "top": 80,
                    "right": 900,
                    "bottom": 700,
                    "width": 800,
                    "height": 620,
                },
            }
        )

        self.assertEqual(WECHAT_WINDOW_GAP, 12)
        self.assertEqual(result, (912, 80))

    def test_invalid_or_missing_probe_keeps_operating_system_default(self):
        self.assertIsNone(position_to_right_of_wechat(None))
        self.assertIsNone(position_to_right_of_wechat({"ok": False}))
        self.assertIsNone(
            position_to_right_of_wechat(
                {
                    "ok": True,
                    "geometry": {
                        "right": 900,
                        "top": 80,
                        "width": 0,
                        "height": 620,
                    },
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
