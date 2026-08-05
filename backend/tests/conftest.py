import os
from pathlib import Path
import tempfile

import pytest
from fastapi import Request


# Production defaults to the real OmniAuto runtime. Unit/API tests explicitly
# select the deterministic adapter and never make provider calls.
os.environ.setdefault("C3_AI_ADAPTER_MODE", "mock")
os.environ.setdefault("C3_BATCH_RECOVERY_POLL_SECONDS", "0")
test_db_path = Path(tempfile.gettempdir()) / f"chejin-backend-tests-{os.getpid()}.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{test_db_path}")
vehicle_image_root = Path(tempfile.gettempdir()) / f"chejin-vehicle-images-{os.getpid()}"
os.environ.setdefault("VEHICLE_IMAGE_STORAGE_ROOT", str(vehicle_image_root))


@pytest.fixture(autouse=True)
def authenticated_admin_dependency(request):
    """Keep business tests focused while auth tests exercise the real dependency."""
    from app.core.auth import require_admin_auth
    from app.main import app

    if request.node.get_closest_marker("real_auth"):
        app.dependency_overrides.pop(require_admin_auth, None)
        yield
        return

    def test_admin(http_request: Request):
        http_request.state.auth_actor = {
            "operator_id": "00000000-0000-0000-0000-000000000001",
            "operator_name": "Ops Tester",
            "actor_type": "admin_account",
            "session_id": "test-session",
        }

    app.dependency_overrides[require_admin_auth] = test_admin
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_admin_auth, None)
