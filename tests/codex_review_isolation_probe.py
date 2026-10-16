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
import errno, json, os, pathlib, socket, sys
root = pathlib.Path(sys.argv[1])
assert (root / 'source' / 'allowed.txt').read_text() == 'source-visible'
assert (root / 'source' / 'allowed.txt').stat().st_ino == int(sys.argv[6])
protected = {}
try:
    protected['pid_namespace'] = pathlib.Path('/proc/self/ns/pid').stat().st_ino != int(sys.argv[5])
except OSError:
    protected['pid_namespace'] = False
try:
    data = os.read(int(sys.argv[3]), 4096)
    if data != b'synthetic-descriptor-canary':
        raise RuntimeError('descriptor positive-control input was not carried through')
except OSError as error:
    protected['descriptor_closed'] = error.errno == errno.EBADF
else:
    protected['descriptor_closed'] = False
for label, path in [('private_file', root / 'private' / 'sentinel'),
                    ('symlink', root / 'source' / 'private-link'),
                    ('credential_environment', pathlib.Path('/proc/self/environ')),
                    ('ancestor_filesystem', pathlib.Path('/proc') / sys.argv[4] / 'root' / str(root / 'private' / 'sentinel').lstrip('/'))]:
    try:
        data = path.read_bytes()
        if label == 'credential_environment':
            protected[label] = b'synthetic-descriptor-canary' not in data
            continue
    except PermissionError:
        protected[label] = True
    except FileNotFoundError:
        protected[label] = label != 'ancestor_filesystem' or protected['pid_namespace']
    else:
        protected[label] = False
try:
    with socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2):
        pass
except OSError:
    protected['network'] = True
else:
    protected['network'] = False
print(json.dumps(protected, sort_keys=True))
sys.exit(0 if all(protected.values()) else 1)
'''


def permission_args(private: Path) -> list[str]:
    """Use the pinned CLI's real profile syntax, including proc/descriptor denial."""
    # /dev/fd is a symlink into /proc, not an independent mount target.
    # Request canonical proc denial; test credential boundaries directly because
    # the backend can still expose safe proc metadata.
    denied = (private.resolve(), Path('/proc'), Path('/sys'), Path('/run'))
    entries = ','.join(json.dumps(str(path)) + '="deny"' for path in denied)
    return ['-c', 'default_permissions="review_probe"', '-c',
            'permissions.review_probe={extends=":read-only",filesystem={' + entries + '}}',
            '-c', 'approval_policy="never"',
            '-c', 'shell_environment_policy.inherit="none"',
            '-c', 'shell_environment_policy.set={PATH="/usr/local/bin:/usr/bin:/bin",LANG="C.UTF-8"}']


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
            environment.update(CODEX_HOME=str(private), HOME=str(root), TMPDIR=str(temporary),
                               REVIEW_PROBE_CANARY=DESCRIPTOR_CANARY)
            prefix = [str(codex.resolve()), *permission_args(private)]
            program = ['sandbox', '--', 'python3', '-c', PROBE, str(root), str(port)]
            try:
                with (private / 'sentinel').open('rb') as descriptor:
                    arguments = program + [str(descriptor.fileno()), str(os.getpid()), str(Path('/proc/self/ns/pid').stat().st_ino),
                                 str((source / 'allowed.txt').stat().st_ino)]
                    result = subprocess.run(prefix + arguments, cwd=source,
                                            env=environment, text=True, capture_output=True,
                                            stdin=subprocess.DEVNULL, pass_fds=(descriptor.fileno(),),
                                            timeout=timeout, check=False)
                    descriptor.seek(0)
                    control = subprocess.run(prefix + ['-c', 'shell_environment_policy.inherit="all"'] + arguments,
                                             cwd=source, env=environment, text=True, capture_output=True,
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
            expected = {'private_file', 'symlink', 'credential_environment', 'descriptor_closed', 'pid_namespace', 'ancestor_filesystem', 'network'}
            shaped = isinstance(checks, dict) and set(checks) == expected and all(type(v) is bool for v in checks.values())
            try:
                control_checks = json.loads(control.stdout)
            except (ValueError, TypeError):
                control_checks = None
            control_shaped = isinstance(control_checks, dict) and set(control_checks) == expected and all(type(v) is bool for v in control_checks.values())
            canary_visible = control_shaped and control_checks['credential_environment'] is False and control.returncode == 1 and control.stderr == ''
            sentinel_intact = (private / 'sentinel').read_text() == DESCRIPTOR_CANARY
            valid = shaped and all(checks.values()) and canary_visible and sentinel_intact
            return {'qualified': result.returncode == 0 and valid,
                    'reason': 'qualified_tool_boundary_checks' if result.returncode == 0 and valid else 'sandbox_qualification_failed',
                    'exit_code': result.returncode, 'checks': checks if shaped else None,
                    'environment_control_canary_visible': canary_visible, 'host_sentinel_intact': sentinel_intact,
                    'environment_control_checks': control_checks if control_shaped else None,
                    # Synthetic-only process. Retain startup errors for diagnosis.
                    'diagnostic': result.stderr[-1800:] if result.returncode else '',
                    'scope': 'observed tool file access/environment filtering/ancestor/descriptor/PID-namespace/symlink/network checks only; not model, MCP, workflow or credential-source acceptance'}


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
