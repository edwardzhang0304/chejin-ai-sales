"""Compatibility alias for vision-owned product image fingerprints."""

import importlib as _importlib
import sys as _sys

_impl = _importlib.import_module("apps.wechat_ai_customer_service.optional_plugins.vision.vehicle_retrieval.fingerprint")
_sys.modules[__name__] = _impl
