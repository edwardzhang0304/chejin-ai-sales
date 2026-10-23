"""Backend artifact version; independent of the C2 wire contract revision."""
from functools import lru_cache
from pathlib import Path
import re
import tomllib


@lru_cache(maxsize=1)
def application_version():
    # Docker copies the same pyproject next to app; local source uses backend/.
    metadata = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    value = tomllib.loads(metadata.read_text())['project']['version']
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise RuntimeError('INVALID_BACKEND_APPLICATION_VERSION')
    return value
