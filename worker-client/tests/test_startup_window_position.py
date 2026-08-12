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
            },
            window_size=(316, 628),
            screen_bounds=(0, 0, 1920, 1080),
        )

        self.assertEqual(WECHAT_WINDOW_GAP, 12)
        self.assertEqual(result, (912, 80))

    def test_invalid_or_missing_probe_keeps_operating_system_default(self):
        self.assertIsNone(position_to_right_of_wechat(
            None,
            window_size=(316, 628),
            screen_bounds=(0, 0, 1920, 1080),
        ))
        self.assertIsNone(position_to_right_of_wechat(
            {"ok": False},
            window_size=(316, 628),
            screen_bounds=(0, 0, 1920, 1080),
        ))
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
                },
                window_size=(316, 628),
                screen_bounds=(0, 0, 1920, 1080),
            )
        )

    def test_uses_left_side_when_wechat_is_near_display_right_edge(self):
        result = position_to_right_of_wechat(
            {
                "ok": True,
                "geometry": {
                    "left": 900,
                    "top": 80,
                    "right": 1900,
                    "bottom": 800,
                    "width": 1000,
                    "height": 720,
                },
            },
            window_size=(316, 628),
            screen_bounds=(0, 0, 1920, 1080),
        )

        self.assertEqual(result, (572, 80))

    def test_clamps_inside_offset_display(self):
        result = position_to_right_of_wechat(
            {
                "ok": True,
                "geometry": {
                    "left": -1200,
                    "top": 900,
                    "right": -400,
                    "bottom": 1500,
                    "width": 800,
                    "height": 600,
                },
            },
            window_size=(316, 628),
            screen_bounds=(-1280, 0, 0, 1024),
        )

        self.assertEqual(result, (-388, 396))


if __name__ == "__main__":
    unittest.main()
