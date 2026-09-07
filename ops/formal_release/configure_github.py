"""Operator command: configure two branch-restricted environments after approval.

Host, pinned known_hosts and dedicated key file paths come from a private JSON file.
Secret values are passed on stdin, never in command arguments or printed output.
"""
import argparse
import json
from pathlib import Path
import subprocess

from verify import require


def call(args, payload=None):
    result = subprocess.run(['gh', *args], input=payload, text=True, capture_output=True)
    require(result.returncode == 0, 'GITHUB_CONFIGURATION_FAILED')
    return json.loads(result.stdout) if result.stdout.strip() and args[0] == 'api' else None


def configure(repo, config):
    require(repo.count('/') == 1 and all(part and all(c.isalnum() or c in '._-' for c in part) for part in repo.split('/')), 'INVALID_REPOSITORY')
    branch = 'codex/gray-release-0.9.x'
    for environment, role in (('formal-staging','stage'), ('formal-production','promote')):
        endpoint = f'repos/{repo}/environments/{environment}'
        call(['api','--method','PUT',endpoint,'--input','-'], json.dumps({
            'deployment_branch_policy':{'protected_branches':False,'custom_branch_policies':True}}))
        existing = call(['api',endpoint+'/deployment-branch-policies'])['branch_policies']
        require(all(policy['name'] == branch and policy['type'] == 'branch' for policy in existing), 'UNEXPECTED_ENVIRONMENT_BRANCH_POLICY')
        if not existing:
            call(['api','--method','POST',endpoint+'/deployment-branch-policies','--input','-'], json.dumps({'name':branch,'type':'branch'}))
        values = {'FORMAL_SSH_HOST':config['host'], 'FORMAL_SSH_PORT':str(config['port']),
                  'FORMAL_KNOWN_HOSTS':Path(config['known_hosts_file']).read_text(),
                  'FORMAL_SSH_KEY':Path(config[role+'_key_file']).read_text()}
        for name, value in values.items():
            require(value.strip(), 'EMPTY_SECRET')
            call(['secret','set',name,'--repo',repo,'--env',environment],value)
        for name, value in (('FORMAL_API_ORIGIN',config['api_origin']), ('FORMAL_DOWNLOAD_ORIGIN',config['download_origin'])):
            call(['variable','set',name,'--repo',repo,'--env',environment,'--body',value])
        print(environment + ': configured (secret values suppressed)')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--approved-receiver-setup',action='store_true',required=True)
    args=parser.parse_args()
    configure(args.repo,json.loads(args.config.read_text()))
