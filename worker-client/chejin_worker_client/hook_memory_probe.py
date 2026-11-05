"""Explicit engineering entry, separate from passive desktop ZIP export.

Uses deployment config and the original gateway, never guessed ports, credentials
or a second HTTP implementation. No looping or automatic retry/release.
"""
import argparse
import json

from .hook_config import load_hook_deployment
from .hook_gateway import HookGateway
from .single_instance import acquire_single_instance


def main(argv=None):
    parser = argparse.ArgumentParser(description='One explicit DLL memory probe; original evidence is retained.')
    parser.add_argument('operation', choices=('stats', 'trim-working-set'))
    parser.add_argument('--confirm-working-set-only', action='store_true',
                        help='Explicitly permit one SwapMemory call; this is not a leak repair.')
    args = parser.parse_args(argv)
    if args.operation == 'trim-working-set' and not args.confirm_working_set_only:
        parser.error('SwapMemory requires --confirm-working-set-only; it changes working set, not allocation ownership.')
    route = 'DiySceneStats' if args.operation == 'stats' else 'SwapMemory'
    # Uses the same protected data home/lease as Worker. Never launches Worker,
    # WeChat, login, callback registration, business tasks or a performance sampler.
    guard = acquire_single_instance()
    try:
        deployment = load_hook_deployment()
        gateway = HookGateway(deployment, timeout_seconds=5, max_response_bytes=64 * 1024,
                              lock_timeout_seconds=1)
        process = gateway.inspect()
        summary = gateway.memory_diagnostic(process, route, operator_requested=True)
    finally:
        guard.release()
    # Full safe receipt is already in the shared transport ledger and ZIP export.
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
