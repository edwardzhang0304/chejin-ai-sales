"""Operator-installed manual download publication. Never registers an automatic update."""
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from verify import identity, digest, require, manual_acceptance


def check_approval(approval, meta, delivery):
    from build_admin import validate_browser_receipt
    require(all(approval.get(k) == meta[k] for k in ('version','commit','sha256')), 'PRODUCTION_APPROVAL_IDENTITY_MISMATCH')
    require(approval.get('release_route') == 'manual' and approval.get('manual_install_accepted') is True
            and approval.get('button_upgrade_accepted') is False and approval.get('workers_safe') is True
            and approval.get('backend_ready') is True and approval.get('old_nginx_workers_drained') is True,
            'MANUAL_PRODUCTION_APPROVAL_REQUIRED')
    if approval.get('approved_read_flows'):
        require(delivery.get('pending_read_install_check') == 'passed', 'RECOVERY_ACCEPTANCE_REQUIRED')
    require(approval['admin_candidate'].get('environment') == 'production' and approval['admin_candidate'].get('api_base') == '/api', 'PRODUCTION_ADMIN_REQUIRED')
    validate_browser_receipt(approval['admin_candidate'], approval['admin_browser_receipt'])


def publish(folder, meta, verified, config, check_only):
    from maintenance import BLOCK, INCLUDE, SNIPPET, SITE, validate_ingress, atomic
    from receiver import run_fixed
    stem,_ = identity(meta)
    delivery = json.loads((folder/(stem+'.delivery.json')).read_text(encoding='utf-8-sig'))
    require(manual_acceptance(delivery), 'MANUAL_ACCEPTANCE_REQUIRED')
    approval = json.loads((folder/'production-approval.json').read_text())
    check_approval(approval, meta, delivery)
    require(SNIPPET.exists() and SNIPPET.read_text()==BLOCK and INCLUDE in SITE.read_text(), 'PERSISTENT_MAINTENANCE_REQUIRED')
    validate_ingress()
    require(run_fixed(['docker','inspect',config['container'],'--format','{{.State.Health.Status}}'],text=True).strip()=='healthy','BACKEND_UNHEALTHY')
    # Separate immutable operator code from its JSON input. No scripts from artifacts execute.
    check = Path(__file__).with_name('manual_readiness.py').read_text()
    request = {'contract_revision':verified['contract_revision'],'contract_sha256':verified['contract_sha256'],
               'approved_read_flows':approval.get('approved_read_flows',{})}
    result=json.loads(run_fixed(['docker','exec',config['container'],'python','-c',check,json.dumps(request)],text=True))
    require(result.get('ready') is True,'MANUAL_BACKEND_NOT_READY')
    origin=config.get('manual_download_origin'); root=config.get('manual_download_root'); sitefile=config.get('manual_download_site')
    require(isinstance(origin,str) and urlsplit(origin).scheme=='https' and urlsplit(origin).hostname and urlsplit(origin).path in ('','/')
            and not urlsplit(origin).username and not urlsplit(origin).query and not urlsplit(origin).fragment,
            'MANUAL_DOWNLOAD_ORIGIN_NOT_CONFIGURED')
    require(root and sitefile,'MANUAL_DOWNLOAD_SERVER_NOT_CONFIGURED')
    site=Path(sitefile).resolve(); before=site.read_text()
    marker='    location ^~ /api/client-releases/artifacts/ {'
    require(before.count(marker)==1,'MANUAL_DOWNLOAD_LOCATION_NOT_CONFIGURED')
    destination=Path(root)/'releases'/meta['version']
    block=''
    for suffix in ('.zip','.sha256.txt'):
        name=stem+suffix; source=folder/name
        require(digest(source)==meta['files'][name]['sha256'],'MANUAL_ARTIFACT_CHANGED')
        location=f'/releases/{meta["version"]}/{name}'
        block+=f'    location = {location} {{\n        limit_except GET HEAD {{ deny all; }}\n        try_files $uri =404;\n        add_header Content-Disposition "attachment" always;\n        add_header Cache-Control "public, max-age=300" always;\n        add_header X-Content-Type-Options "nosniff" always;\n    }}\n\n'
        if (destination/name).exists():require(digest(destination/name)==digest(source),'IMMUTABLE_VERSION_CONFLICT')
    if not check_only:
        destination.mkdir(parents=True,exist_ok=True,mode=0o755)
        for suffix in ('.zip','.sha256.txt'):
            source=folder/(stem+suffix); target=destination/source.name
            if not target.exists():
                temp=target.with_suffix(target.suffix+'.partial');shutil.copyfile(source,temp);temp.chmod(0o644);os.replace(temp,target)
        if block not in before:
            require(f'location = /releases/{meta["version"]}/' not in before,'MANUAL_LOCATION_CONFLICT')
            atomic(site,before.replace(marker,block+marker,1))
            try:run_fixed(['nginx','-t']);run_fixed(['systemctl','reload','nginx'])
            except Exception:
                atomic(site,before)
                # Restore valid routing only; no application/data rollback is performed.
                run_fixed(['nginx','-t']);run_fixed(['systemctl','reload','nginx']);raise
    return {**verified,'backend':'passed','publication':'manual_published' if not check_only else 'not_run',
            'installation_mode':'preserve_data_manual_install','automatic_update_registered':False,
            'url':origin.rstrip('/')+'/releases/'+meta['version']+'/'+stem+'.zip','external_download':'not_run'}
