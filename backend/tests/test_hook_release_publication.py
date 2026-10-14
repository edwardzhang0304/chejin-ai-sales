from pathlib import Path
from dataclasses import replace
from urllib.parse import urlsplit
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from app.core.database import Base,SessionLocal,engine
from app.services.client_release_service import register_signed_client_release,store_client_release_artifact
from chejin_worker_client.models import ClientRelease
from chejin_worker_client.release_package_contract import validate_release_contract,verify_release_signature,load_trusted_release_keys
from test_client_release_registration import _signed_descriptor


def setup_function():
    Base.metadata.drop_all(bind=engine);Base.metadata.create_all(bind=engine)


def test_real_signer_publication_and_queries_isolate_ocr_and_native(tmp_path,monkeypatch):
    settings=get_settings();monkeypatch.setattr(settings,'client_release_artifact_root',str(tmp_path/'stored'))
    monkeypatch.setattr(settings,'client_release_public_base_url','https://testserver/api')
    public={}
    for transport,channel in [('ocr','gray'),('hook','gray-hook')]:
        area=tmp_path/transport;area.mkdir()
        version='0.10.2' if transport=='hook' else '0.10.1'
        descriptor,keys=_signed_descriptor(area,transport=transport,version=version)
        with SessionLocal.begin() as db:
            release=register_signed_client_release(db,descriptor,public_keys_path=keys)
            store_client_release_artifact(release,area/f'CheJinWorkerClient-v{version}-{channel}-windows-x64.zip')
        public[channel]=keys
    http=TestClient(app)
    old=http.get('/api/client-releases/latest',params={'current_version':'0.9.69','platform':'windows-x64','channel':'gray-hook'})
    assert old.status_code==409 and old.json()['code']=='UPDATE_MANUAL_UPGRADE_REQUIRED'
    for channel in ['gray','gray-hook']:
        current='0.10.1' if channel=='gray-hook' else '0.9.69'
        response=http.get('/api/client-releases/latest',params={'current_version':current,'platform':'windows-x64','channel':channel})
        assert response.status_code==200
        value=ClientRelease.from_api(response.json()['data'])
        assert value.update_available and value.channel==channel
        verify_release_signature(value,trusted_keys=load_trusted_release_keys(public[channel]))
        from chejin_worker_client.client_update import extract_verified_archive
        from chejin_worker_client.release_package_contract import verify_staged_package,ClientUpdateError
        url=urlsplit(value.artifact_url);download=http.get(url.path+'?'+url.query)
        assert download.status_code==200
        archive=tmp_path/(channel+'.zip');archive.write_bytes(download.content)
        package=extract_verified_archive(archive,tmp_path/(channel+'-staged'))
        assert verify_staged_package(value,package)['transport']==('hook' if channel=='gray-hook' else 'ocr')
        validate_release_contract(value,current_version=current,expected_channel=channel)
        other='gray' if channel=='gray-hook' else 'gray-hook'
        from chejin_worker_client.release_package_contract import ClientUpdateError
        with pytest.raises(ClientUpdateError):validate_release_contract(value,current_version=current,expected_channel=other)
        with pytest.raises(ClientUpdateError):verify_staged_package(replace(value,channel=other),package)
        with pytest.raises(ClientUpdateError):verify_release_signature(replace(value,channel=other),trusted_keys=load_trusted_release_keys(public[channel]))
    response=http.get('/api/client-releases/latest',params={'current_version':'0.9.69','platform':'windows-x64','channel':'unrecognized'})
    assert response.status_code==400
