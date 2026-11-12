#!/usr/bin/env python3
"""Record one PR observation without changing original task custody or budgets.

Uses the existing POSIX checkpoint lock. No provider, scheduler or credentials.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

import sys


def main():
    path = Path('.ai/sessions/active-task.json')
    owner, repo = os.environ['WATCH_OWNER'], os.environ['WATCH_REPOSITORY']
    number, head, phase = (os.environ[key] for key in ('WATCH_PR', 'WATCH_HEAD', 'WATCH_PHASE'))
    if (not re.fullmatch(r'[A-Za-z0-9_.:/#-]{1,256}', owner)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or not re.fullmatch(r'[1-9][0-9]{0,9}', number)
            or not re.fullmatch(r'[0-9a-f]{40}', head)
            or phase not in {'watching', 'merged', 'closed', 'verifying'}):
        raise ValueError('Trusted owner and native PR observation required')
    # Require the original checkpoint; never create a replacement task on a new host.
    if any(item.is_symlink() for item in (Path('.ai'), path.parent, path, path.with_name(path.name + '.lock'))):
        raise ValueError('Checkpoint paths must remain within the accepted workspace')
    if not path.is_file():
        raise ValueError('Original task checkpoint unavailable; retain its owner')
    with path.with_name(path.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        task = json.loads(path.read_text())
        if not isinstance(task, dict):
            raise ValueError('Invalid checkpoint; reconcile in its existing owner')
        custody = task.get('continuation', task)
        if not isinstance(custody, dict) or custody.get('owner') != owner:
            raise ValueError('Accepted task owner differs or is unknown')
        if ('continuation' in task and (type(custody.get('version')) is not int or custody.get('version') != 1)
                or not isinstance(custody.get('repository', repo), str)
                or custody.get('repository', repo).casefold() != repo.casefold()):
            raise ValueError('Task repository differs')
        watches = task.setdefault('pr_watches', {})
        if not isinstance(watches, dict):
            raise ValueError('Unknown retained watch format')
        key = repo.casefold() + '#' + number
        previous = watches.get(key)
        if previous is not None and (not isinstance(previous, dict) or previous.get('owner') != owner):
            raise ValueError('Retain existing PR owner')
        if previous is not None and (not isinstance(previous.get('repository', repo), str)
                or previous.get('repository', repo).casefold() != repo.casefold()
                or type(previous.get('pr', int(number))) is not int
                or previous.get('pr', int(number)) != int(number)):
            raise ValueError('Retain existing PR scope; reconcile in its owner')
        if previous is None and len(watches) >= 100:
            raise ValueError('Watch capacity reached; retain unfinished work')
        observed = dict(previous or {}, owner=owner, repository=repo, pr=int(number), head=head, phase=phase)
        if previous != observed:
            watches[key] = observed
            temporary = None
            try:
                with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(task, stream, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError):
        print("PR observation unavailable; retain the accepted owner and original checkpoint.", file=sys.stderr)
        raise SystemExit(2)
