#!/usr/bin/env python3
"""Record PR observations without writing the original task checkpoint.

No provider, scheduler or credentials. Call from the verified command bootstrap.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def _same_scope(row, owner, repo, number):
    return (isinstance(row, dict) and row.get('owner') == owner
            and isinstance(row.get('repository'), str)
            and row['repository'].casefold() == repo.casefold()
            and type(row.get('pr')) is int and row['pr'] == int(number))


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
    namespace = hashlib.sha256((owner + '\0' + repo.casefold()).encode()).hexdigest()
    target = path.with_name(path.name + '.pr-watches-' + namespace + '.json')
    lock_path = target.with_name(target.name + '.lock')
    if any(item.is_symlink() for item in (Path('.ai'), path.parent, path, target, lock_path)):
        raise ValueError('Checkpoint paths must remain within the accepted workspace')
    if not path.is_file():
        raise ValueError('Original checkpoint unavailable; retain its owner')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Parent writers may use another protocol. This reader never writes or
        # replaces the parent, so it cannot discard their concurrent updates.
        task = json.loads(path.read_text())
        if not isinstance(task, dict):
            raise ValueError('Invalid original checkpoint')
        custody = task.get('continuation', task)
        if not isinstance(custody, dict) or custody.get('owner') != owner:
            raise ValueError('Accepted task owner differs or is unknown')
        if 'continuation' in task and (type(custody.get('version')) is not int
                or custody['version'] != 1 or 'repository' not in custody):
            raise ValueError('Versioned custody requires an explicit repository')
        bound_repo = custody.get('repository', repo)
        if not isinstance(bound_repo, str) or bound_repo.casefold() != repo.casefold():
            raise ValueError('Original repository differs')
        legacy = task.get('pr_watches', {})
        if not isinstance(legacy, dict):
            raise ValueError('Unknown legacy observation format')
        key = repo.casefold() + '#' + number
        if key in legacy and not _same_scope(legacy[key], owner, repo, number):
            raise ValueError('Retain the existing PR owner/scope')
        if target.exists():
            state = json.loads(target.read_text())
            if (not isinstance(state, dict) or type(state.get('version')) is not int
                    or state['version'] != 1 or state.get('owner') != owner
                    or not isinstance(state.get('repository'), str)
                    or state['repository'].casefold() != repo.casefold()
                    or not isinstance(state.get('watches'), dict)):
                raise ValueError('Retain existing observation custody')
        else:
            state = dict(version=1, owner=owner, repository=repo, watches={})
        watches = state['watches']
        previous = watches.get(key)
        if key in watches and not _same_scope(previous, owner, repo, number):
            raise ValueError('Retain existing PR owner/scope')
        if len(set(legacy) | set(watches) | {key}) > 100:
            raise ValueError('Observation capacity reached; retain unfinished work')
        observed = dict(previous or {}, owner=owner, repository=repo, pr=int(number), head=head, phase=phase)
        if previous == observed:
            return
        watches[key] = observed
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError):
        print('PR observation unavailable; retain the accepted owner and original checkpoint.', file=sys.stderr)
        raise SystemExit(2)
