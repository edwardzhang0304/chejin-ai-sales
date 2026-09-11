"""Persistently fence public grant endpoints; receipts remain available. Linux operator CLI."""
import argparse, hashlib, json, os, re
from pathlib import Path
import subprocess, time

SNIPPET = Path('/etc/nginx/snippets/chejin-release-maintenance.conf')
SITE = Path('/etc/nginx/sites-enabled/jiangsuchejin.com.conf')
BLOCK_PATTERN = r'^/api/(tasks/[^/]+/claim|workers/[^/]+/(run-status|inflight-flow/start)|reply-actions/[^/]+/claim-send)(/|$)'
BLOCK = 'if ($uri ~ "' + BLOCK_PATTERN + '") { return 503 \'{"code":"RELEASE_MAINTENANCE","message":"发布维护中，暂不领取新任务，请稍后重试。","data":{"retryable":true}}\'; }\n'
INCLUDE = '    include /etc/nginx/snippets/chejin-release-maintenance.conf;\n'

def blocked_path(path):
    import re
    from urllib.parse import unquote, urlsplit
    import posixpath
    return bool(re.match(BLOCK_PATTERN, posixpath.normpath(unquote(urlsplit(path).path))))

def atomic(path, data):
    temp=path.with_name(path.name+'.tmp');temp.write_text(data);temp.chmod(0o644);os.replace(temp,path)

def old_workers():
    r=subprocess.run(['ps','-eo','pid,args'],capture_output=True,text=True,check=True)
    return {int(l.strip().split(None,1)[0]) for l in r.stdout.splitlines() if 'nginx: worker process' in l}

def validate_ingress():
    state=json.loads(subprocess.check_output(['docker','inspect','chejin-leads-api']))[0]
    assert state['HostConfig']['PortBindings']=={'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'8000'}]},'BACKEND_INGRESS_NOT_LOCAL_ONLY'
    for p in Path('/etc/nginx/sites-enabled').iterdir():
        if p.resolve()==SITE.resolve():continue
        s=p.read_text()
        # The existing download vhost permits artifacts only and explicitly denies other API paths.
        assert p.name=='update.jiangsuchejin.com.conf' and 'location ^~ /api/client-releases/artifacts/' in s and re.search(r'location\s+\^~\s+/api/\s*\{\s*return\s+(403|404);\s*\}', s),'UNREVIEWED_API_INGRESS'

def main():
    ap=argparse.ArgumentParser();ap.add_argument('operation',choices=['enable','status','disable']);ap.add_argument('--evidence-dir',required=True);a=ap.parse_args()
    evidence=Path(a.evidence_dir);evidence.mkdir(parents=True,exist_ok=True)
    active=SNIPPET.exists() and SNIPPET.read_text()==BLOCK
    if a.operation=='status':
        validate_ingress();assert active and INCLUDE in SITE.read_text(),'MAINTENANCE_NOT_ENABLED'
        print(json.dumps({'maintenance':'active','scope':'public_grant_endpoints','persistent':True}));return
    validate_ingress();site=SITE.resolve();before=site.read_text()
    if a.operation=='enable':
        assert not active,'Already enabled; inspect status'
        previous=SNIPPET.read_text() if SNIPPET.exists() else ''
        assert previous in ('','# release maintenance inactive\n'),'Unknown existing maintenance snippet'
        if INCLUDE not in before:
            marker='    location = /api {';assert before.count(marker)==1,'Unexpected main API location'
            atomic(site,before.replace(marker,INCLUDE+marker,1))
        atomic(SNIPPET,BLOCK)
    else:
        assert active and (evidence/'release-approved.json').exists(),'Explicit accepted release evidence required to unfreeze'
        approved=json.loads((evidence/'release-approved.json').read_text())
        assert approved.get('backend_ready') and approved.get('admin_browser_gate')=='passed' and approved.get('workers_safe'),'Release acceptance incomplete'
        from build_admin import validate_browser_receipt
        validate_browser_receipt(approved['admin_candidate'], approved['admin_browser_receipt'])
        atomic(SNIPPET,'# release maintenance inactive\n')
    try:subprocess.run(['nginx','-t'],capture_output=True,check=True)
    except Exception:
        atomic(site,before);atomic(SNIPPET,previous if a.operation=='enable' else BLOCK);raise
    try:
        old=old_workers();subprocess.run(['systemctl','reload','nginx'],check=True)
        until=time.monotonic()+60
        while old.intersection(old_workers()):
            if time.monotonic()>until:raise RuntimeError('OLD_NGINX_WORKERS_NOT_DRAINED')
            time.sleep(1)
    except Exception:
        # A failed unfreeze must not leave an inactive persistent configuration.
        atomic(SNIPPET,BLOCK)
        subprocess.run(['nginx','-t'],capture_output=True,check=True)
        subprocess.run(['systemctl','reload','nginx'],check=True)
        raise RuntimeError('RELOAD_OR_DRAIN_FAILED; freeze restored; verify ingress before continuing')
    result={'maintenance':'active' if a.operation=='enable' else 'inactive','persistent':True,'old_workers_drained':True,'scope':'public_grant_endpoints','snippet_sha256':hashlib.sha256(SNIPPET.read_bytes()).hexdigest()}
    (evidence/('maintenance-'+a.operation+'.json')).write_text(json.dumps(result,indent=2));print(json.dumps(result))
if __name__=='__main__':main()
