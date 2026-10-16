#!/usr/bin/env python3
"""Model-free CI qualification of a candidate tool sandbox, never live auth."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


DESCRIPTOR_CANARY = 'synthetic-descriptor-canary'


PROBE = r'''
import json, pathlib, socket, sys
root = pathlib.Path(sys.argv[1])
assert (root / 'source' / 'allowed.txt').read_text() == 'source-visible'
denied = {}
for label, path in [('private_file', root / 'private' / 'sentinel'),
                    ('symlink', root / 'source' / 'private-link'),
                    ('process_environment', pathlib.Path('/proc/self/environ')),
                    ('descriptor_alias', pathlib.Path('/dev/fd') / sys.argv[3])]:
    try:
        data = path.read_bytes()
        if label == 'descriptor_alias' and data != b'synthetic-descriptor-canary':
            raise RuntimeError('descriptor positive-control input was not carried through')
    except (PermissionError, FileNotFoundError):
        denied[label] = True
    else:
        denied[label] = False
try:
    with socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2):
        pass
except OSError:
    denied['network'] = True
else:
    denied['network'] = False
print(json.dumps(denied, sort_keys=True))
sys.exit(0 if all(denied.values()) else 1)
'''


def permission_args(private: Path) -> list[str]:
    """Use the pinned CLI's real profile syntax, including proc/descriptor denial."""
    # /dev/fd is a symlink into /proc, not an independent mount target.
    # Mask the canonical proc tree and test its descriptor alias explicitly.
    denied = (private.resolve(), Path('/proc'), Path('/sys'), Path('/run'))
    entries = ','.join(json.dumps(str(path)) + '="deny"' for path in denied)
    return ['-c', 'default_permissions="review_probe"', '-c',
            'permissions.review_probe={extends=":read-only",filesystem={' + entries + '}}',
            '-c', 'approval_policy="never"']


def qualify(codex: Path, *, timeout: int = 30) -> dict:
    """A startup failure or malformed result is failed qualification, never pass."""
    with tempfile.TemporaryDirectory(prefix='codex-isolation-') as directory:
        root = Path(directory)
        private, source, temporary = (root / part for part in ('private', 'source', 'tmp'))
        for path in (private, source, temporary):
            path.mkdir(mode=0o700)
        # No credential is fetched, copied, inspected or passed to the CLI.
        (private / 'sentinel').write_text(DESCRIPTOR_CANARY)
        (source / 'allowed.txt').write_text('source-visible')
        (source / 'private-link').symlink_to(private / 'sentinel')
        # The control proves the host can reach the server, so a failed sandbox
        # connection proves isolation rather than an absent endpoint.
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(2)
            port = server.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                peer, _ = server.accept()
                peer.close()
            environment = {field: os.environ[field] for field in ('PATH', 'LANG', 'LC_ALL') if field in os.environ}
            environment.update(CODEX_HOME=str(private), HOME=str(root), TMPDIR=str(temporary))
            command = [str(codex.resolve()), *permission_args(private), 'sandbox', '--',
                       'python3', '-c', PROBE, str(root), str(port)]
            try:
                with (private / 'sentinel').open('rb') as descriptor:
                    result = subprocess.run(command + [str(descriptor.fileno())], cwd=source,
                                            env=environment, text=True, capture_output=True,
                                            stdin=subprocess.DEVNULL, pass_fds=(descriptor.fileno(),),
                                            timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                return {'qualified': False, 'reason': 'sandbox_timeout'}
            except OSError as exc:
                return {'qualified': False, 'reason': 'sandbox_unavailable', 'error_type': type(exc).__name__}
            try:
                checks = json.loads(result.stdout)
            except (ValueError, TypeError):
                checks = None
            expected = {'private_file', 'symlink', 'process_environment', 'descriptor_alias', 'network'}
            valid = isinstance(checks, dict) and set(checks) == expected and all(v is True for v in checks.values())
            return {'qualified': result.returncode == 0 and valid,
                    'reason': 'qualified_tool_sandbox' if result.returncode == 0 and valid else 'sandbox_qualification_failed',
                    'exit_code': result.returncode, 'checks': checks if valid else None,
                    # Synthetic-only process. Retain startup errors for diagnosis.
                    'diagnostic': result.stderr[-1800:] if result.returncode else '',
                    'scope': 'tool file/proc/descriptor/symlink/network isolation only; not model, MCP, workflow or credential-source acceptance'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'codex': str(args.codex), 'output': str(args.output),
                          'uses_live_credentials': False, 'makes_model_calls': False}))
        return 0
    try:
        report = qualify(args.codex)
    except OSError as exc:
        report = {'qualified': False, 'reason': 'probe_control_setup_failed',
                  'error_type': type(exc).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))
    return 0 if report['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
