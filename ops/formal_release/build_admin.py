"""Build the exact frontend with an explicit environment; require browser evidence separately."""
import argparse, hashlib, json, os, re, subprocess
from pathlib import Path
from urllib.parse import urlsplit

def validate_api(environment, api_base):
    if environment=='production':
        if api_base!='/api':raise ValueError('PRODUCTION_REQUIRES_SAME_ORIGIN_API')
    elif environment=='fast-uat':
        u=urlsplit(api_base)
        if u.scheme not in {'http','https'} or not u.hostname or u.hostname.endswith('jiangsuchejin.com') or u.path!='/api' or u.query or u.fragment or u.username or u.password:
            raise ValueError('UAT_REQUIRES_EXPLICIT_TEST_API')
    else:raise ValueError('ENVIRONMENT_REQUIRED')

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def validate_browser_receipt(candidate, receipt):
    if receipt.get('environment')!=candidate['environment'] or receipt.get('frontend_files')!=candidate['files'] or receipt.get('api_base')!=candidate['api_base']:
        raise ValueError('BROWSER_EVIDENCE_BUILD_MISMATCH')
    for key in ('login','session_after_refresh','readonly_business_page','logout'):
        if receipt.get(key)!='passed':raise ValueError('BROWSER_GATE_INCOMPLETE')
    expected='https://jiangsuchejin.com/api' if candidate['environment']=='production' else candidate['api_base']
    if receipt.get('actual_api_base')!=expected or not receipt.get('observed_at'):
        raise ValueError('BROWSER_DESTINATION_UNVERIFIED')
    return True

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--environment',required=True,choices=['production','fast-uat']);ap.add_argument('--api-base',required=True);ap.add_argument('--source-root',required=True);ap.add_argument('--output',required=True);a=ap.parse_args()
    from component_release import environment_stage
    root=Path(a.source_root).resolve()
    plan=environment_stage('admin_build', root)
    validate_api(a.environment,a.api_base);front=root/'frontend';out=Path(a.output)
    if plan:
        from component_release import git, require
        tracked = set(git(root, 'ls-files', '-z', 'frontend').decode().split('\0'))
        require(all(p.relative_to(root).as_posix() in tracked for p in front.glob('.env*')),
                'UNREVIEWED_FRONTEND_ENV_FILE')
    env={**os.environ,'VITE_API_BASE_URL':a.api_base}
    # Explicit CLI value overrides inherited Vite env files; no secret value is logged.
    subprocess.run(['npm','run','build'],cwd=front,env=env,check=True)
    files={str(p.relative_to(front/'dist')):sha(p) for p in (front/'dist').rglob('*') if p.is_file()}
    assert 'index.html' in files
    javascript='\n'.join(p.read_text() for p in (front/'dist/assets').glob('*.js'))
    # This source uses import.meta.env; verify Vite replaced its actual config entry.
    pattern=r'VITE_API_BASE_URL\s*:\s*'+re.escape(json.dumps(a.api_base))
    if not re.search(pattern,javascript):raise ValueError('COMPILED_API_CONFIGURATION_MISSING')
    result={'environment':a.environment,'api_base':a.api_base,'files':files,'package_lock_sha256':sha(front/'package-lock.json'),'browser_gate':'pending'}
    if plan: result.update(source_commit=plan['source_commit'], component_plan_sha256=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode()).hexdigest())
    out.write_text(json.dumps(result,indent=2));print('ADMIN_CANDIDATE_CONFIG_VERIFIED_BROWSER_GATE_PENDING')
if __name__=='__main__':main()
