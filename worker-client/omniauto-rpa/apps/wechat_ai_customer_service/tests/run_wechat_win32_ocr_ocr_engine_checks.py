"""Contract checks for Win32/OCR OCR engine extraction."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import ocr_engine  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


OCR_ROWS = [
    ([[0, 0], [100, 0], [100, 20], [0, 20]], "  文件  传输助手  ", 0.98),
    ([[0, 30], [80, 30], [80, 50], [0, 50]], "", 0.99),
    ([[0, 60], [80, 60], [80, 80], [0, 80]], "低置信", 0.2),
    ([[100, 60], [180, 60], [180, 80], [100, 80]], "许聪", "bad"),
    ("bad-row",),
]


class FakeEngine:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return self.rows, None


def test_ocr_engine_module_exports_expected_helpers() -> None:
    for name in ("normalize_ocr_rows", "run_ocr_with_cache", "OcrEngineRunner", "create_ocr_runner"):
        assert_true(hasattr(ocr_engine, name), f"ocr helper missing: {name}")


def test_normalize_ocr_rows_matches_sidecar_with_fake_engine() -> None:
    original_rapid = sidecar.RapidOCR
    original_engine = sidecar._OCR_ENGINE
    fake = FakeEngine(OCR_ROWS)
    try:
        sidecar.RapidOCR = lambda: fake
        sidecar._OCR_ENGINE = None
        sidecar_items = sidecar.run_ocr(object())
    finally:
        sidecar.RapidOCR = original_rapid
        sidecar._OCR_ENGINE = original_engine
    extracted_items = ocr_engine.normalize_ocr_rows(OCR_ROWS)
    assert_true(extracted_items == sidecar_items, f"ocr row normalization mismatch: {extracted_items} != {sidecar_items}")


def test_ocr_runner_matches_sidecar_with_fake_engine() -> None:
    original_rapid = sidecar.RapidOCR
    original_engine = sidecar._OCR_ENGINE
    sidecar_fake = FakeEngine(OCR_ROWS)
    runner_fake = FakeEngine(OCR_ROWS)
    try:
        sidecar.RapidOCR = lambda: sidecar_fake
        sidecar._OCR_ENGINE = None
        sidecar_items = sidecar.run_ocr(object())
    finally:
        sidecar.RapidOCR = original_rapid
        sidecar._OCR_ENGINE = original_engine
    runner = ocr_engine.create_ocr_runner(lambda: runner_fake)
    extracted_items = runner.run(object())
    assert_true(extracted_items == sidecar_items, f"ocr runner mismatch: {extracted_items} != {sidecar_items}")
    runner.run(object())
    assert_true(runner_fake.calls == 2, f"runner should cache engine instance but call it per image: {runner_fake.calls}")


def test_run_ocr_with_cache_matches_sidecar_cache_contract() -> None:
    original_rapid = sidecar.RapidOCR
    original_engine = sidecar._OCR_ENGINE
    sidecar_fake = FakeEngine(OCR_ROWS)
    extracted_fake = FakeEngine(OCR_ROWS)
    try:
        sidecar.RapidOCR = lambda: sidecar_fake
        sidecar._OCR_ENGINE = None
        first_sidecar = sidecar.run_ocr(object())
        sidecar_cached = sidecar._OCR_ENGINE
        second_sidecar = sidecar.run_ocr(object())
    finally:
        sidecar.RapidOCR = original_rapid
        sidecar._OCR_ENGINE = original_engine
    first_extracted, extracted_cached = ocr_engine.run_ocr_with_cache(
        object(),
        engine_factory=lambda: extracted_fake,
        engine=None,
    )
    second_extracted, extracted_cached_again = ocr_engine.run_ocr_with_cache(
        object(),
        engine_factory=lambda: extracted_fake,
        engine=extracted_cached,
    )
    assert_true(first_extracted == first_sidecar, f"first cached OCR mismatch: {first_extracted} != {first_sidecar}")
    assert_true(second_extracted == second_sidecar, f"second cached OCR mismatch: {second_extracted} != {second_sidecar}")
    assert_true(sidecar_cached is sidecar_fake, "sidecar should cache the created fake engine")
    assert_true(extracted_cached is extracted_fake, "extracted helper should return the created fake engine")
    assert_true(extracted_cached_again is extracted_cached, "extracted helper should reuse the passed cached engine")
    assert_true(sidecar_fake.calls == 2, f"sidecar fake should be called twice through one cached engine: {sidecar_fake.calls}")
    assert_true(extracted_fake.calls == 2, f"extracted fake should be called twice through one cached engine: {extracted_fake.calls}")


def test_ocr_runner_unavailable_error_matches_sidecar_shape() -> None:
    runner = ocr_engine.create_ocr_runner(None, import_error="unit_missing")
    try:
        runner.run(object())
    except RuntimeError as exc:
        assert_true(
            str(exc) == "rapidocr_onnxruntime_unavailable: unit_missing",
            f"unexpected unavailable error: {exc}",
        )
    else:
        raise AssertionError("expected unavailable OCR runner to raise")


def test_foreign_overlay_rows_filter_to_empty() -> None:
    rows = [
        ([[0, 0], [100, 0], [100, 20], [0, 20]], "apps/wechat_ai_customer_servic", 0.99),
        ([[0, 30], [100, 30], [100, 50], [0, 50]], "文件已更改", 0.99),
    ]
    assert_true(ocr_engine.normalize_ocr_rows(rows) == [], "foreign overlay OCR rows should be filtered")


def main() -> int:
    tests = [
        test_ocr_engine_module_exports_expected_helpers,
        test_normalize_ocr_rows_matches_sidecar_with_fake_engine,
        test_ocr_runner_matches_sidecar_with_fake_engine,
        test_run_ocr_with_cache_matches_sidecar_cache_contract,
        test_ocr_runner_unavailable_error_matches_sidecar_shape,
        test_foreign_overlay_rows_filter_to_empty,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR OCR engine checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
