"""Fail before expensive formal builds if authorization or receiver configuration is absent."""
import os
from verify import SHA, VERSION, require


def validate(env):
    require(env.get('GITHUB_REF') == 'refs/heads/codex/gray-release-0.9.x', 'RELEASE_BRANCH_REQUIRED')
    require(env.get('RELEASE_APPROVED') == 'true' and env.get('RELEASE_REASON', '').strip(), 'RELEASE_APPROVAL_REQUIRED')
    mode = env.get('DELIVERY_MODE')
    require(mode in {'build_and_stage','retest_candidate','stage_existing','check_staged','publish_staged'}, 'INVALID_MODE')
    require(VERSION.fullmatch(env.get('CURRENT_VERSION','')), 'INVALID_CURRENT_VERSION')
    require(tuple(map(int, env['CURRENT_VERSION'].split('.'))) >= (0, 9, 69), 'UNSUPPORTED_UPGRADE_START')
    require(env['CURRENT_VERSION'] == '0.9.72', 'UPGRADE_START_FIXTURE_NOT_CONFIGURED')
    for name in ('FORMAL_SSH_KEY','FORMAL_SSH_HOST','FORMAL_SSH_PORT','FORMAL_KNOWN_HOSTS'):
        require(env.get(name, '').strip(), 'FORMAL_RECEIVER_NOT_CONFIGURED')
    if mode == 'stage_existing':
        require(env.get('FORMAL_RUN_ID','').isdigit(), 'FORMAL_RUN_REQUIRED')
    if mode == 'retest_candidate':
        require(env.get('CANDIDATE_RUN_ID','').isdigit(), 'CANDIDATE_RUN_REQUIRED')
    if mode in {'check_staged','publish_staged'}:
        require(SHA.fullmatch(env.get('STAGE_ID','')) and env.get('PRODUCTION_READY') == 'true', 'PRODUCTION_APPROVAL_REQUIRED')


if __name__ == '__main__':
    try:
        validate(os.environ)
        print('FORMAL_DISPATCH_VALIDATED')
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
