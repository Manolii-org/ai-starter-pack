"""Stable CLI; structured metadata on stdout, no raw provider or sender errors."""
import argparse
import json
import time

from .profile import QualityError, load
from .provider import Unspam
from .runner import remaining, send, start, wait
from .state import State


def main(argv=None):
    parser = argparse.ArgumentParser(prog='email-quality')
    parser.add_argument('--profile', required=True)
    parser.add_argument('--state')
    parser.add_argument('--sender')
    parser.add_argument('--kind', choices=('spam', 'inbox'))
    parser.add_argument('command', choices=('status', 'start', 'wait', 'run'))
    args = parser.parse_args(argv)
    began = time.monotonic()
    receipt = {'schema_version': '1.0', 'operation': args.command, 'provider': 'unspam'}
    state = None
    try:
        profile = load(args.profile)
        deadline = began + profile['timeout_seconds']
        receipt['scope'] = {k: profile[k] for k in ('org', 'repository', 'environment')}
        receipt['account_mode'] = profile['account_mode']
        client = Unspam()
        if args.command == 'status':
            receipt.update(outcome='ready', remaining=client.status(timeout=remaining(deadline)))
        else:
            if not args.state or (args.command != 'run' and args.kind not in profile['checks']):
                raise QualityError('CONFIG_INVALID')
            if args.command == 'run' and not args.sender:
                raise QualityError('SENDER_INVALID')
            state = State(args.state, profile)
            results = []
            receipt['results'] = results
            for kind in profile['checks'] if args.command == 'run' else [args.kind]:
                remaining(deadline)
                if args.command in ('start', 'run'):
                    record = start(client, state, profile, kind, deadline)
                    if args.command == 'run':
                        send(state, record, args.sender, remaining(deadline), deadline=deadline)
                if args.command in ('wait', 'run'):
                    results.append(wait(client, state, profile, kind, deadline))
            receipt.update(outcome=('started' if args.command == 'start' else
                                    'passed' if all(r['passed'] for r in results) else 'assertion_failed'),
                           results=results)
    except QualityError as error:
        receipt.update(outcome='blocked', error_code=error.code)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        receipt.update(outcome='blocked', error_code='LOCAL_OR_PROVIDER_INVALID')
    finally:
        if state:
            state.close()
    receipt['duration_seconds'] = round(time.monotonic() - began, 3)
    print(json.dumps(receipt, sort_keys=True, allow_nan=False))
    return 0 if receipt.get('outcome') in ('passed', 'ready', 'started') else 1
