"""Private run checkpoints: exact-scope resume and fail-closed mutation intents."""
import fcntl
import json
import os
import stat
from pathlib import Path

from .profile import QualityError, digest


def private_write(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())


class State:
    def __init__(self, directory, profile):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise QualityError('PRIVATE_STATE_REQUIRED')
        self.lock = os.open(self.directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            raise QualityError('RUN_BUSY') from None
        self.path = self.directory / 'state.json'
        try:
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise QualityError('PRIVATE_STATE_REQUIRED')
                self.data = json.loads(self.path.read_text())
                if self.data.get('profile_digest') != digest(profile):
                    raise QualityError('STATE_SCOPE_MISMATCH')
            else:
                self.data = {'profile_digest': digest(profile), 'checks': {}}
        except QualityError:
            self.close()
            raise
        except (OSError, ValueError, AttributeError):
            self.close()
            raise QualityError('PRIVATE_STATE_REQUIRED') from None

    def save(self):
        temporary = self.directory / 'state.tmp'
        private_write(temporary, self.data)
        os.replace(temporary, self.path)

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
