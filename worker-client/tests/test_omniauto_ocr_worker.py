from __future__ import annotations

import base64
import io
import unittest
from unittest.mock import patch

from PIL import Image

from chejin_worker_client.omniauto_ocr_worker import process_request
from apps.wechat_ai_customer_service.adapters import (
    wechat_win32_ocr_sidecar,
)


class OmniAutoOcrWorkerTest(unittest.TestCase):
    def test_request_decodes_in_memory_image_and_uses_omniauto_ocr(self):
        source = Image.new("RGB", (80, 40), "white")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        source.close()
        seen_sizes = []

        def run_ocr(image):
            seen_sizes.append(image.size)
            return [
                {
                    "text": "CJR8S5K3",
                    "left": 1,
                    "top": 2,
                    "right": 30,
                    "bottom": 12,
                }
            ]

        with patch.object(wechat_win32_ocr_sidecar, "run_ocr", run_ocr):
            result = process_request(
                {
                    "image_base64": base64.b64encode(
                        buffer.getvalue()
                    ).decode("ascii")
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(seen_sizes, [(80, 40)])
        self.assertEqual(result["items"][0]["text"], "CJR8S5K3")

    def test_request_rejects_missing_image(self):
        with self.assertRaisesRegex(
            ValueError,
            "OMNIAUTO_OCR_WORKER_REQUEST_INVALID",
        ):
            process_request({})


if __name__ == "__main__":
    unittest.main()
