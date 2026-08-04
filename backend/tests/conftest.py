import os
from pathlib import Path
import tempfile


# Production defaults to the real OmniAuto runtime. Unit/API tests explicitly
# select the deterministic adapter and never make provider calls.
os.environ.setdefault("C3_AI_ADAPTER_MODE", "mock")
os.environ.setdefault("C3_BATCH_RECOVERY_POLL_SECONDS", "0")
test_db_path = Path(tempfile.gettempdir()) / f"chejin-backend-tests-{os.getpid()}.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{test_db_path}")
