"""Manual publication must preserve the route, approval and immutable package boundary."""
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ops/formal_release'))
import manual_publication as mp
import deliver
from verify import manual_acceptance


def approval(meta):
    candidate={'environment':'production','api_base':'/api','files':{'index.html':'a'*64}}
    receipt={'environment':'production','api_base':'/api','frontend_files':candidate['files'],
             'actual_api_base':'https://jiangsuchejin.com/api','observed_at':'2026-09-12T00:00:00Z',
             **{k:'passed' for k in ('login','session_after_refresh','readonly_business_page','logout')}}
    return {**{k:meta[k] for k in ('version','commit','sha256')}, 'release_route':'manual',
            'manual_install_accepted':True,'button_upgrade_accepted':False,'workers_safe':True,
            'backend_ready':True,'old_nginx_workers_drained':True,'admin_candidate':candidate,'admin_browser_receipt':receipt}


@pytest.mark.parametrize('field,value', [('version','different'),('release_route','button'),
    ('manual_install_accepted',False),('button_upgrade_accepted',True),('backend_ready',False)])
def test_wrong_approval_blocks_publication(field,value):
    meta={'version':'2.4.9','commit':'a'*40,'sha256':'b'*64};a=approval(meta);a[field]=value
    with pytest.raises(ValueError):mp.check_approval(a,meta,{})


def test_manual_requires_production_browser_and_recovery_proof():
    meta={'version':'2.4.9','commit':'a'*40,'sha256':'b'*64};a=approval(meta)
    a['approved_read_flows']={'worker':'f'*64}
    with pytest.raises(ValueError,match='RECOVERY_ACCEPTANCE'):mp.check_approval(a,meta,{})
    mp.check_approval(a,meta,{'pending_read_install_check':'passed'})
    a['admin_candidate']['environment']='fast-uat'
    with pytest.raises(ValueError,match='PRODUCTION_ADMIN'):mp.check_approval(a,meta,{'pending_read_install_check':'passed'})


def test_cli_dry_check_does_not_download_unpublished_file(tmp_path):
    with patch.object(sys,'argv',['deliver','check-manual','--current-version','2.4.8','--stage-id','x','--workers-drained','--result',str(tmp_path/'r.json')]), patch.object(deliver,'Remote') as remote, patch.object(deliver,'verify_external') as auto, patch.object(deliver,'verify_manual_external') as manual:
        remote.return_value.return_value={'publication':'not_run','installation_mode':'preserve_data_manual_install'}
        deliver.main()
        auto.assert_not_called();manual.assert_not_called()


def test_cli_manual_publish_uses_manual_verifier(tmp_path,monkeypatch):
    monkeypatch.setenv('FORMAL_API_ORIGIN','https://api.test/api');monkeypatch.setenv('FORMAL_DOWNLOAD_ORIGIN','https://download.test')
    with patch.object(sys,'argv',['deliver','publish-manual','--current-version','2.4.8','--stage-id','x','--workers-drained','--result',str(tmp_path/'r.json')]), patch.object(deliver,'Remote') as remote, patch.object(deliver,'verify_external') as auto, patch.object(deliver,'verify_manual_external',return_value={}) as manual:
        remote.return_value.return_value={'publication':'manual_published','installation_mode':'preserve_data_manual_install'}
        deliver.main()
        auto.assert_not_called();manual.assert_called_once()


def test_manual_check_publish_and_idempotent_repeat_preserve_bytes(tmp_path):
    # Exercise real files; only production subprocesses and prior package verification are stubbed.
    stem='chejin-worker-v2.4.9-windows-x64';folder=tmp_path/'stage';folder.mkdir()
    files={}
    for suffix,body in (('.zip',b'fixture package'),('.sha256.txt',b'fixture checksum')):
        p=folder/(stem+suffix);p.write_bytes(body);files[p.name]={'sha256':hashlib.sha256(body).hexdigest()}
    meta={'version':'2.4.9','commit':'a'*40,'sha256':files[stem+'.zip']['sha256'],'files':files}
    (folder/(stem+'.delivery.json')).write_text('{}');(folder/'production-approval.json').write_text(json.dumps(approval(meta)))
    site=tmp_path/'site';site.write_text('server {\n    location ^~ /api/client-releases/artifacts/ {\n    }\n}\n')
    snippet=tmp_path/'maintenance';snippet.write_text('active');ingress=tmp_path/'ingress';ingress.write_text('include')
    config={'container':'fixture','manual_download_origin':'https://download.test','manual_download_root':str(tmp_path/'www'),'manual_download_site':str(site)}
    verified={'contract_revision':'2.4.9','contract_sha256':'f'*64}
    def command(args,**kwargs):return 'healthy' if args[:2]==['docker','inspect'] else '{"ready":true}'
    with patch.object(mp,'identity',return_value=(stem,None)), patch.object(mp,'manual_acceptance',return_value=True), patch('maintenance.SNIPPET',snippet), patch('maintenance.BLOCK','active'), patch('maintenance.INCLUDE','include'), patch('maintenance.SITE',ingress), patch('maintenance.validate_ingress'), patch('receiver.run_fixed',side_effect=command) as run:
        before=site.read_bytes();mp.publish(folder,meta,verified,config,True)
        assert site.read_bytes()==before and not (tmp_path/'www').exists()
        result=mp.publish(folder,meta,verified,config,False)
        assert result['automatic_update_registered'] is False
        published=site.read_bytes();mp.publish(folder,meta,verified,config,False)
        assert site.read_bytes()==published
        assert sum(c.args[0]==['systemctl','reload','nginx'] for c in run.call_args_list)==1
        target=tmp_path/'www/releases/2.4.9'/ (stem+'.zip');target.write_bytes(b'different')
        with pytest.raises(ValueError,match='IMMUTABLE_VERSION_CONFLICT'):mp.publish(folder,meta,verified,config,False)


def test_manual_external_rejects_automatic_offer_and_wrong_bytes():
    body=b'package';result={'url':'https://download.test/releases/package.zip','size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
    class Response(io.BytesIO):
        status=200
        def __init__(self,body):super().__init__(body);self.headers={'Content-Length':str(len(body))}
    def attempt(offer,artifact):
        def open_url(url,**kwargs):
            if 'latest?' in url:return Response(json.dumps({'data':{'update_available':offer}}).encode())
            return Response(artifact if url==result['url'] else b'healthy')
        with patch.object(deliver,'build_opener') as opener:
            opener.return_value.open.side_effect=open_url
            return deliver.verify_manual_external(result,'2.4.8','https://api.test/api','https://download.test')
    assert attempt(False,body)['external_download']=='passed'
    with pytest.raises(ValueError,match='ADVERTISES_AUTOMATIC'):attempt(True,body)
    with pytest.raises(ValueError,match='HASH_MISMATCH'):attempt(False,b'corrupt')
