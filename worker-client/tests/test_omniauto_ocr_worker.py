from __future__ import annotations

import base64
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

from chejin_worker_client.omniauto_ocr_worker import process_request
from chejin_worker_client.subprocess_protocol import (
    UNICODE_PROTOCOL_SENTINEL,
    encode_subprocess_json,
    subprocess_utf8_environment,
)
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
                    "text": "复制",
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
                    ).decode("ascii"),
                    "protocol_unicode_sentinel": UNICODE_PROTOCOL_SENTINEL,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(seen_sizes, [(80, 40)])
        self.assertEqual(result["items"][0]["text"], "复制")
        self.assertEqual(
            result["protocol_unicode_sentinel"],
            UNICODE_PROTOCOL_SENTINEL,
        )

    def test_pipe_json_is_ascii_safe_and_preserves_chinese(self):
        encoded = encode_subprocess_json(
            {
                "protocol_unicode_sentinel": UNICODE_PROTOCOL_SENTINEL,
                "items": [{"text": "复制图片"}],
            }
        )

        self.assertTrue(encoded.isascii())
        decoded = json.loads(encoded)
        self.assertEqual(decoded["items"][0]["text"], "复制图片")
        self.assertEqual(
            decoded["protocol_unicode_sentinel"],
            UNICODE_PROTOCOL_SENTINEL,
        )

    def test_child_environment_explicitly_uses_utf8(self):
        environment = subprocess_utf8_environment()

        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHONUTF8"], "1")

    def test_request_rejects_missing_image(self):
        with self.assertRaisesRegex(
            ValueError,
            "OMNIAUTO_OCR_WORKER_REQUEST_INVALID",
        ):
            process_request({})


if __name__ == "__main__":
    unittest.main()
