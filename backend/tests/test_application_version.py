from pathlib import Path
import tomllib

from app.version import application_version


def test_backend_version_endpoint_uses_artifact_version_not_contract(monkeypatch):
    from app.api.routes.auth_session import system_version
    from app.contracts import c2
    from app import version
    expected = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())['project']['version']
    assert application_version() == expected
    monkeypatch.setattr(c2, 'contract_revision', lambda: '0.0.1')
    monkeypatch.setattr(version, 'application_version', lambda: '0.10.99')
    assert system_version()['data']['product_version'] == '0.10.99'
    from app.main import create_app
    assert create_app().version == '0.10.99'
